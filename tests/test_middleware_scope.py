"""Middleware composes at compile time and also wraps 404/405 in its scope."""

from typing import cast

import pytest

from stario import App, Route
from stario.exceptions import StarioError
from stario.http.context import Context, Handler
from stario.http.writer import Writer
from stario.testing import TestClient
from tests.helpers import track


def _record(calls: list[str], label: str):
    return track(calls, label)


async def test_middleware_wraps_default_404_under_its_prefix() -> None:
    calls: list[str] = []

    async def handler(_c: Context, w: Writer) -> None:
        w.text("ok")

    app = App()
    app.use("/api", _record(calls, "api"))
    app.add(Route("GET /api/users"), handler)

    async with TestClient(app) as client:
        response = await client.get("/api/missing")

    assert response.status_code == 404
    assert response.text == "Not Found"
    assert calls == ["api"]


async def test_middleware_wraps_custom_not_found() -> None:
    calls: list[str] = []

    async def custom_404(_c: Context, w: Writer) -> None:
        w.text("api 404", 404)

    app = App()
    app.use("/api", _record(calls, "api"))
    app.not_found("/api", custom_404)

    async with TestClient(app) as client:
        response = await client.get("/api/missing")

    assert response.status_code == 404
    assert response.text == "api 404"
    assert calls == ["api"]


async def test_middleware_wraps_405_under_its_prefix() -> None:
    calls: list[str] = []

    async def handler(_c: Context, w: Writer) -> None:
        w.text("ok")

    app = App()
    app.use("/api", _record(calls, "api"))
    app.add(Route("GET /api/users"), handler)

    async with TestClient(app) as client:
        response = await client.post("/api/users")

    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"
    assert calls == ["api"]


async def test_middleware_does_not_run_outside_its_prefix() -> None:
    calls: list[str] = []
    app = App()
    app.use("/api", _record(calls, "api"))

    async with TestClient(app) as client:
        response = await client.get("/other/missing")

    assert response.status_code == 404
    assert calls == []


async def test_root_middleware_wraps_custom_root_not_found_once() -> None:
    calls: list[str] = []

    async def custom_404(_c: Context, w: Writer) -> None:
        w.text("custom 404", 404)

    app = App()
    app.use("/", _record(calls, "root"))
    app.not_found("/", custom_404)

    async with TestClient(app) as client:
        response = await client.get("/missing")

    assert response.status_code == 404
    assert response.text == "custom 404"
    assert calls == ["root"]


async def test_host_route_composition_order_is_unchanged() -> None:
    calls: list[str] = []

    async def handler(_c: Context, w: Writer) -> None:
        w.text("ok")

    app = App()
    app.use("//{sub}.example.com", _record(calls, "a"))
    app.use("/", _record(calls, "b"))
    app.add(
        Route("GET //{sub}.example.com/x"),
        handler,
        middleware=[_record(calls, "c")],
    )

    async with TestClient(app) as client:
        response = await client.get("/x", headers={"host": "acme.example.com"})

    assert response.status_code == 200
    assert calls == ["a", "b", "c"]


async def test_route_registered_after_first_lookup_gets_scope_middleware() -> None:
    calls: list[str] = []

    async def first(_c: Context, w: Writer) -> None:
        w.text("first")

    async def second(_c: Context, w: Writer) -> None:
        w.text("second")

    app = App()
    app.use("/api", _record(calls, "api"))
    app.add(Route("GET /api/first"), first)

    async with TestClient(app) as client:
        assert (await client.get("/api/first")).status_code == 200
        app.add(Route("GET /api/second"), second)
        response = await client.get("/api/second")

    assert response.status_code == 200
    assert calls == ["api", "api"]


async def test_composed_handler_is_checked_for_async() -> None:
    def bad_middleware(_handler: Handler) -> Handler:
        return cast(Handler, lambda c, w: None)

    async def handler(_c: Context, w: Writer) -> None:
        w.text("ok")

    app = App()
    app.use("/api", bad_middleware)
    app.add(Route("GET /api/x"), handler)

    with pytest.raises(StarioError, match="Composed handler"):
        app.compiled()
