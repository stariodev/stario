"""In-process async HTTP test client on the production protocol."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin
from uuid import UUID

from stario.http.app import App
from stario.http.bootstrap import AppRun, Bootstrap
from stario.http.compression import CompressionConfig
from stario.http.config import DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT, RequestPolicy
from stario.http.drain import cancel_pending, record_cancelled
from stario.http.headers import Headers
from stario.telemetry.core import Span
from stario.testing.cookies import merge_cookie_jar
from stario.testing.encode import decode_content_encoding, parse_response_cookies
from stario.testing.load import aload_run
from stario.testing.models import ClientRequest
from stario.testing.request import merge_headers, prepare_request
from stario.testing.response import TestResponse, TestStreamResponse
from stario.testing.tracer import TestTracer
from stario.testing.types import (
    CookieMap,
    FileData,
    FormData,
    HeaderMap,
    QueryParamInput,
)
from stario.testing.wire import Exchange, open_exchange, request_bytes
from stario_cython.protocol import Connections


@dataclass(slots=True, frozen=True)
class TestExchange:
    __test__ = False

    request: ClientRequest
    response: TestResponse


class TestClient:
    """Async HTTP client that drives an `App` through the real server protocol.

    Pass a wired `App` or the bootstrap async generator your program uses in
    production. Enter `async with TestClient(...)` first; `app` is then the
    live application. Every request is serialized to HTTP/1.1 bytes and fed to
    the same `HttpProtocol` that `serve()` runs, on an in-memory
    connection. Routing, 308 / 400 target answers, framing, compression, and
    the request span all come from production code.

    Buffered requests — `get` / `post` / … return a `TestResponse` once the
    handler has finished: the whole body is in `content` and the request span
    is closed, so `client.tracer.get_span(response.span_id)` works right away.
    Redirects are followed up to `max_redirects` unless overridden per call.

    Streaming — `stream` yields a `TestStreamResponse` as soon as headers are
    in; read with `iter_bytes` / `iter_events`. Leaving the block hangs up
    (the handler sees `c.disconnected`) and waits for the handler to finish.

    `timeout` / `request_timeout` cap one exchange. Past it the client hangs
    up, cancels that handler, and raises `TimeoutError`.

    Exit — open connections close, the `server.shutdown` span opens, the
    app's shutdown is signaled (so `app.shutting_down` work loops wake), and
    `drain_tasks` runs up to `shutdown_timeout`; tasks still pending past it
    are cancelled, like the server's graceful shutdown. Each cancel is a
    `server.shutdown.task_cancelled` event with the task name. Then bootstrap
    teardown (if any) matches normal app shutdown.
    """

    __test__ = False

    def __init__(
        self,
        app_or_bootstrap: App | Bootstrap,
        *,
        app_factory: Callable[[], App] | None = None,
        owns_shutdown: bool = True,
        base_url: str = "http://testserver",
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 20,
        compression: CompressionConfig | None = None,
        requests: RequestPolicy | None = None,
        request_timeout: float | None = 30.0,
        shutdown_timeout: float | None = DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
    ) -> None:
        """Wire the client.

        - `app_or_bootstrap`: Built `App` or production bootstrap async generator.
        - `app_factory`: Optional factory when using a bootstrap (default `App`).
        - `owns_shutdown`: When passing a built `App`, whether exiting the client
          signals `app.shutdown` (default `True`). Set `False` when an outer
          `aload_app` or `Server` owns the app's shutdown future.
        - `base_url`: Origin for relative URLs (default `http://testserver`).
        - `headers`: Default headers merged into every request.
        - `cookies`: Initial cookie jar (`dict`-like).
        - `follow_redirects`: Default for buffered requests; ignored by `stream`.
        - `max_redirects`: Buffered redirect cap.
        - `compression`: Server compression policy (default `CompressionConfig()`).
          Responses compress only when the request sends `Accept-Encoding`.
        - `requests`: Server request limits (default `RequestPolicy()`).
        - `request_timeout`: Default seconds per request; `None` means no timeout.
        - `shutdown_timeout`: Seconds exit waits for `app.create_task` work
          after signaling shutdown (default matches the server's
          `STARIO_GRACEFUL_SHUTDOWN_TIMEOUT`, 5 s). Past it, pending tasks are
          cancelled. `None` waits forever — a job that ignores
          `app.shutting_down` then hangs the exit.
        """
        if isinstance(app_or_bootstrap, App):
            self._app = app_or_bootstrap
            self._bootstrap = None
            self._app_factory = None
            self._owns_shutdown = owns_shutdown
        else:
            self._app = None
            self._bootstrap = app_or_bootstrap
            self._app_factory = app_factory
            self._owns_shutdown = False
        self.base_url = base_url.rstrip("/") or "http://testserver"
        self.cookies: dict[str, str] = dict(cookies or {})
        self.default_follow_redirects = follow_redirects
        self.default_headers = Headers()
        merge_headers(self.default_headers, headers)
        self.default_headers.setdefault("user-agent", "stario-testclient")
        self.default_headers.setdefault("accept", "*/*")
        self.exchanges: list[TestExchange] = []
        self.max_redirects = max_redirects
        self.tracer = TestTracer()
        self.compression = (
            compression if compression is not None else CompressionConfig()
        )
        self.requests = requests if requests is not None else RequestPolicy()
        self.request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        # One registry for every connection this client opens. The Server's
        # Date tick would sweep it; here timeouts are off, so it only counts.
        self._connections = Connections(server_driven=True)
        self._date_box = [b""]
        self._async_app_cm: AbstractAsyncContextManager[AppRun] | None = None
        self._run: AppRun | None = None
        self._entered = False

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("TestClient is already entered.")
        self.tracer.__enter__()
        try:
            if self._bootstrap is not None:
                self._async_app_cm = aload_run(
                    self._bootstrap, app_factory=self._app_factory, tracer=self.tracer
                )
                self._run = await self._async_app_cm.__aenter__()
                self._app = self._run.app
            now = format_datetime(datetime.now(UTC), usegmt=True).encode("ascii")
            self._date_box[0] = b"date: " + now + b"\r\n"
            self._entered = True
            return self
        except BaseException:
            self.tracer.__exit__(None, None, None)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            for protocol in list(self._connections):
                transport = protocol.transport
                if transport is not None and not transport.is_closing():
                    transport.close()
            if self._app is not None:
                # Signal before draining so `while not app.shutting_down`
                # loops exit on their own. Bootstrap teardown signals again —
                # signal_shutdown() is idempotent.
                span, owned = self._begin_shutdown(
                    self._app, failed=exc_type is not None
                )
                cancelled = await self._drain(
                    self._shutdown_timeout, open_span=None if span is None else span.id
                )
                if span is not None:
                    span.attr("server.shutdown.cancelled_tasks", len(cancelled))
                    record_cancelled(span, cancelled)
                    if owned:
                        span.end()
            if self._async_app_cm is not None:
                await self._async_app_cm.__aexit__(exc_type, exc_val, exc_tb)
                self._async_app_cm = None
                self._run = None
                self._app = None
        finally:
            self._entered = False
            self.tracer.__exit__(exc_type, exc_val, exc_tb)

    def _begin_shutdown(self, app: App, *, failed: bool) -> tuple[Span | None, bool]:
        """Signal shutdown and open `server.shutdown`, like `Server` before its drain.

        Returns the span and whether this client must end it. A bootstrap run
        owns its span; a built `App` gets one here. With `owns_shutdown=False`
        the outer runner signals and records, so there is no span.
        """
        if self._run is not None:
            self._run.begin_shutdown("runtime_failure" if failed else "expected_stop")
            return self._run.span, False
        if not self._owns_shutdown:
            return None, False
        app.signal_shutdown()
        span = self.tracer.create("server.shutdown")
        span.attr(
            "server.shutdown.trigger", "runtime_failure" if failed else "expected_stop"
        )
        span.start()
        return span, True

    async def drain_tasks(self, timeout: float | None = None) -> None:
        """Wait until `app.tasks` is quiet and `tracer` has no open spans.

        Called automatically after hanging up every connection when exiting
        the client context — the client's `shutdown_timeout` applies there.
        `timeout` caps the wait on `app.create_task` work; tasks still
        pending past it are cancelled, like the server's graceful shutdown.
        `None` waits forever. Unsafe to call from work scheduled via
        `app.create_task` (can deadlock).
        """
        await self._drain(timeout)

    async def _drain(
        self, timeout: float | None, *, open_span: UUID | None = None
    ) -> list[str]:
        """`drain_tasks`, returning the names of the tasks it cancelled.

        `open_span` is the `server.shutdown` span the exit holds open while it
        drains; the wait for open spans skips it.
        """
        if not self._entered:
            raise RuntimeError(
                "TestClient must be entered before drain_tasks(). Use `async with TestClient(...)`."
            )
        app = self._app
        if app is None:
            raise RuntimeError(
                "TestClient has no wired app (enter `async with TestClient(...)` before drain_tasks)."
            )
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(timeout, 0.0)
        cancelled: list[str] = []
        while True:
            # Poll the set so tasks scheduled while draining still count.
            while True:
                pending = [task for task in app.tasks if not task.done()]
                if not pending:
                    break
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    cancelled += await cancel_pending(pending)
                    break
                wait_timeout = 0.05 if remaining is None else min(0.05, remaining)
                await asyncio.wait(
                    pending,
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            if not self.tracer.has_open_spans(besides=open_span):
                return cancelled
            await asyncio.sleep(0)

    @property
    def app(self) -> App:
        """The running `App` (available inside `async with TestClient(...)`)."""
        if not self._entered:
            raise RuntimeError(
                "TestClient must be used as an async context manager. "
                "Use `async with TestClient(...) as client:` before calling request helpers."
            )
        if self._app is None:
            raise RuntimeError(
                "TestClient was given a bootstrap function but is not inside `async with TestClient(...)`. "
                "Enter the context manager before calling await .get() / .post() / …."
            )
        return self._app

    # --- one connection -------------------------------------------------

    def _open(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None,
        headers: HeaderMap | None,
        cookies: CookieMap | None,
        json: Any | None,
        data: FormData | bytes | str | None,
        files: FileData | None,
        content: bytes | str | None,
    ) -> tuple[Exchange, ClientRequest]:
        pm, purl, ppath, pqs, phdrs, pbody = prepare_request(
            method=method,
            url=url,
            base_url=self.base_url,
            base_headers=self.default_headers,
            request_headers=headers,
            client_cookies=self.cookies,
            request_cookies=cookies,
            params=params,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        policy = self.requests
        exchange = open_exchange(
            app=self.app,
            tracer=self.tracer,
            date_box=self._date_box,
            compression=self.compression,
            connections=self._connections,
            policy={
                "max_header_bytes": policy.max_header_bytes,
                "max_body_bytes": policy.max_body_bytes,
                "header_timeout": policy.header_timeout,
                "keep_alive_timeout": policy.keep_alive_timeout,
                "body_timeout": policy.body_timeout,
                "max_pipelined_requests": policy.max_pipelined_requests,
                "write_timeout": policy.write_timeout,
            },
            raw=request_bytes(pm, ppath, pqs, phdrs, pbody),
            head_request=pm == "HEAD",
        )
        request = ClientRequest(
            method=pm,
            url=purl,
            path=ppath,
            query_string=pqs,
            headers=phdrs,
            content=pbody,
        )
        return exchange, request

    def _deadline(self, timeout: float | None) -> float | None:
        seconds = self.request_timeout if timeout is None else timeout
        if seconds is None:
            return None
        return asyncio.get_running_loop().time() + seconds

    async def _timed_out(
        self, exchange: Exchange, seconds: float | None
    ) -> TimeoutError:
        """Hang up and cancel the handler, then describe the timeout."""
        exchange.disconnect()
        exchange.cancel()
        if exchange.task is not None:
            await asyncio.gather(exchange.task, return_exceptions=True)
        return TimeoutError(
            f"TestClient: no complete response within {seconds}s "
            f"({exchange.reader.status_code or 'no headers'} so far)."
        )

    # --- streaming ------------------------------------------------------

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[TestStreamResponse]:
        """Start a request and yield `TestStreamResponse` once headers are available.

        Does not follow redirects (inspect `Location` yourself). Leaving the
        block hangs up this connection and waits for the handler coroutine.
        `timeout` caps the whole exchange, including body iteration after
        headers. Send `Accept-Encoding: identity` when reading `iter_bytes`
        so the body is not compressed.
        """
        exchange, request = self._open(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        seconds = self.request_timeout if timeout is None else timeout
        deadline = self._deadline(timeout)
        reader = exchange.reader
        try:
            try:
                status_code, response_headers = await reader.wait_headers(deadline)
            except TimeoutError:
                raise await self._timed_out(exchange, seconds) from None
            merge_cookie_jar(self.cookies, response_headers)
            yield TestStreamResponse(
                status_code=status_code,
                url=request.url,
                headers=response_headers,
                request=request,
                span_id=exchange.span_id,
                cookies=parse_response_cookies(response_headers),
                _exchange=exchange,
                _deadline=deadline,
            )
        finally:
            exchange.disconnect()
            # A handler that ignores the hang-up until the deadline is
            # cancelled; the test's own assertions decide the outcome.
            with contextlib.suppress(TimeoutError):
                await exchange.finish(deadline)

    # --- buffered -------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        """Issue an arbitrary HTTP method and return a fully buffered `TestResponse`."""
        redirect_mode = (
            self.default_follow_redirects
            if follow_redirects is None
            else follow_redirects
        )
        current_method = method.upper()
        current_url = url
        current_params = params
        current_json = json
        current_data = data
        current_files = files
        current_content = content
        current_headers = headers
        current_cookies: dict[str, str] | None = (
            dict(cookies) if cookies is not None else None
        )
        history: list[TestResponse] = []

        for _ in range(self.max_redirects + 1):
            response = await self._send_once(
                current_method,
                current_url,
                params=current_params,
                headers=current_headers,
                cookies=current_cookies,
                json=current_json,
                data=current_data,
                files=current_files,
                content=current_content,
                timeout=timeout,
            )
            response.history = list(history)
            if not redirect_mode or not response.is_redirect:
                return response

            history.append(response)
            current_url = urljoin(response.url, response.headers.get("location", ""))
            current_params = None
            nh = Headers()
            merge_headers(nh, current_headers)
            current_headers = nh

            if response.status_code in {301, 302, 303} and current_method != "HEAD":
                current_method = "GET"
                current_json = None
                current_data = None
                current_files = None
                current_content = None
                current_headers.remove("content-type")
                current_headers.remove("content-length")
            if current_cookies is not None:
                updated_cookies = dict(current_cookies)
                merge_cookie_jar(updated_cookies, response.headers)
                current_cookies = updated_cookies

        raise RuntimeError(
            f"Too many redirects. Exceeded configured limit of {self.max_redirects}."
        )

    # --- Shortcuts: same `**kwargs` as `request` ---

    async def get(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("GET", url, **kwargs)

    async def query(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("QUERY", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("DELETE", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("OPTIONS", url, **kwargs)

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        exchange, request = self._open(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        seconds = self.request_timeout if timeout is None else timeout
        deadline = self._deadline(timeout)
        reader = exchange.reader
        try:
            # The whole response, then the handler itself: post-response work
            # (`app.create_task` excepted) is done when this returns.
            status_code, response_headers = await reader.wait_headers(deadline)
            body = await reader.body(deadline)
            await exchange.finish(deadline)
        except TimeoutError:
            raise await self._timed_out(exchange, seconds) from None
        finally:
            exchange.disconnect()
        merge_cookie_jar(self.cookies, response_headers)
        response = TestResponse(
            status_code=status_code,
            url=request.url,
            headers=response_headers,
            content=decode_content_encoding(
                body, response_headers.get("content-encoding")
            ),
            request=request,
            span_id=exchange.span_id,
            cookies=parse_response_cookies(response_headers),
        )
        self.exchanges.append(TestExchange(request=request, response=response))
        return response
