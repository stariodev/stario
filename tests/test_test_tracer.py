"""`TestTracer` query helpers on `TestClient`."""

import asyncio

import pytest

from stario import App, Route
from stario.testing import TestClient
from stario.testing import TestTracer as StarioTestTracer


def test_test_tracer_requires_entry_before_create() -> None:
    tracer = StarioTestTracer()

    with pytest.raises(RuntimeError, match="must be entered"):
        tracer.create("request")

    with tracer:
        span = tracer.create("request")
        span.start()
        span.end()


async def _telemetry_bootstrap(app: App, span):
    async def handler(c, w):
        c.span.attr("fixture.user", "ada")
        c.span.event("milestones.started", {"phase": "a"})
        with c.span.step("db.query", {"table": "items"}) as s:
            s.attr("rows", 3)
            s.event("slow", {"ms": 12})
        w.text("ok")

    app.add(Route("GET", "/t"), handler)
    yield


@pytest.mark.asyncio
async def test_test_tracer_queries_via_response_span_id() -> None:
    async with TestClient(_telemetry_bootstrap) as client:
        r = await client.get("/t")
        assert r.status_code == 200
        rid = r.span_id
        t = client.tracer
        assert t.has_attribute(rid, "fixture.user", "ada")
        assert t.has_event(rid, "milestones.started")
        ev = t.get_event(rid, "milestones.started")
        assert ev is not None
        assert ev.attributes.get("phase") == "a"

        child = t.find_span("db.query", root_id=rid)
        assert child is not None
        assert child.attributes.get("table") == "items"
        assert child.attributes.get("rows") == 3
        assert t.get_event(child.id, "slow") is not None

        assert t.get_span(rid) is not None
        assert t.get_span(child.id) is not None


@pytest.mark.asyncio
async def test_find_span_root_id_scopes_to_request_subtree() -> None:
    app = App()

    async def handler(c, w):
        with c.span.step("work") as s:
            s.attr("route", c.req.path)
        w.text("ok")

    app.add(Route("GET", "/a"), handler)
    app.add(Route("GET", "/b"), handler)

    async with TestClient(app) as client:
        r1 = await client.get("/a")
        r2 = await client.get("/b")
        t = client.tracer

        child1 = t.find_span("work", root_id=r1.span_id)
        child2 = t.find_span("work", root_id=r2.span_id)
        assert child1 is not None
        assert child2 is not None
        assert child1.id != child2.id
        assert child1.attributes.get("route") == "/a"
        assert child2.attributes.get("route") == "/b"


@pytest.mark.asyncio
async def test_get_span_returns_none_while_span_open() -> None:
    gate = asyncio.Event()
    app = App()

    async def handler(c, w):
        with c.span.step("slow") as s:
            await gate.wait()
            s.attr("done", True)
        w.text("ok")

    app.add(Route("GET", "/slow"), handler)

    async with TestClient(app) as client:
        task = asyncio.create_task(client.get("/slow"))
        await asyncio.sleep(0)
        assert client.tracer.has_open_spans()
        gate.set()
        r = await task
        assert r.status_code == 200
        assert client.tracer.get_span(r.span_id) is not None


@pytest.mark.asyncio
async def test_test_tracer_reports_message_less_exception_as_error() -> None:
    async def boom(c, w):
        raise ValueError()

    app = App()
    app.add(Route("GET /boom"), boom)
    async with TestClient(app) as client:
        r = await client.get("/boom")
        span = client.tracer.get_span(r.span_id)

    assert r.status_code == 500
    assert span is not None
    assert span.status == "error"
    assert span.error == "ValueError"
