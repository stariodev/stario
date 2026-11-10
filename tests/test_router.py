"""Tests for the trie-based HTTP router."""

import pytest

from stario.exceptions import StarioError
from stario.http import Router, default_not_found, method_not_allowed_handler
from stario.http.context import EMPTY_MATCH, Context, Match
from stario.http.route import EMPTY_ROUTE, Route
from stario.http.writer import Writer
from tests.helpers import DummyWriter, noop_handler, run_handler, track


class TestFindHandler:
    def test_static_find_reuses_the_same_match(self):
        router = Router()
        home = Route("GET /health")
        router.add(home, noop_handler)

        _, _, first = router.find_handler("", "/health", "GET")
        _, _, second = router.find_handler("", "/health", "GET")

        assert first is second
        assert first.pattern == "GET /health"
        assert dict(first.params) == {}
        with pytest.raises(AttributeError, match="immutable"):
            first.pattern = "GET /other"

    def test_matches_path_params(self):
        router = Router()
        router.add(Route("GET", "/users/{user_id}/posts/{post_id}"), noop_handler)

        _, route, hit = router.find_handler("", "/users/42/posts/7", "GET")

        assert route.target == "/users/{user_id}/posts/{post_id}"
        assert dict(hit.params) == {"user_id": "42", "post_id": "7"}
        assert hit.pattern == "GET /users/{user_id}/posts/{post_id}"
        with pytest.raises(TypeError):
            hit.params["user_id"] = "9"  # type: ignore[index]

    def test_param_find_is_stable_without_result_cache(self):
        router = Router()
        router.add(Route("GET /users/{user_id}"), noop_handler)

        _, _, first = router.find_handler("", "/users/42", "GET")
        _, _, second = router.find_handler("", "/users/42", "GET")

        assert first.pattern == second.pattern
        assert dict(first.params) == dict(second.params) == {"user_id": "42"}

    def test_add_after_miss_is_visible(self):
        router = Router()
        handler, _, hit = router.find_handler("", "/later", "GET")
        assert hit is EMPTY_MATCH
        assert handler is default_not_found

        router.add(Route("GET /later"), noop_handler)
        _, route, found = router.find_handler("", "/later", "GET")
        assert found.pattern == "GET /later"
        assert route.target == "/later"

    def test_escaped_braces_register_and_match(self):
        router = Router()
        curly = Route("GET /curly/{{id}}")
        router.add(curly, noop_handler)

        _, route, hit = router.find_handler("", "/curly/{id}", "GET")

        assert route is curly
        assert hit.pattern == "GET /curly/{id}"

    def test_static_chain_and_prefix(self):
        router = Router()
        deep = Route("GET /a/b/c/d/e")
        other = Route("GET /a/b/c/d/f")
        prefix = Route("GET /a/b")
        router.add(deep, noop_handler)
        router.add(other, noop_handler)
        router.add(prefix, noop_handler)

        _, route_e, _ = router.find_handler("", "/a/b/c/d/e", "GET")
        _, route_f, _ = router.find_handler("", "/a/b/c/d/f", "GET")
        _, route_b, _ = router.find_handler("", "/a/b", "GET")

        assert route_e is deep
        assert route_f is other
        assert route_b is prefix

    def test_add_order_does_not_change_static_matches(self):
        deep = Route("GET /a/b/c/d/e")
        other = Route("GET /a/b/c/d/f")
        prefix = Route("GET /a/b")

        def hits(order: list[Route]) -> tuple[Route, Route, Route]:
            router = Router()
            for route in order:
                router.add(route, noop_handler)
            return (
                router.find_handler("", "/a/b/c/d/e", "GET")[1],
                router.find_handler("", "/a/b/c/d/f", "GET")[1],
                router.find_handler("", "/a/b", "GET")[1],
            )

        assert hits([deep, other, prefix]) == (deep, other, prefix)
        assert hits([prefix, other, deep]) == (deep, other, prefix)
        assert hits([other, prefix, deep]) == (deep, other, prefix)

    def test_add_order_does_not_change_exact_vs_param(self):
        users = Route("GET", "/t/{tenant}/users/{id}")
        acme = Route("GET", "/t/acme/users/{id}")

        def hit(first: Route, second: Route) -> tuple[str, str]:
            router = Router()
            router.add(first, noop_handler)
            router.add(second, noop_handler)
            return (
                router.find_handler("", "/t/acme/users/1", "GET")[1].target,
                router.find_handler("", "/t/beta/users/1", "GET")[1].target,
            )

        assert hit(acme, users) == ("/t/acme/users/{id}", "/t/{tenant}/users/{id}")
        assert hit(users, acme) == ("/t/acme/users/{id}", "/t/{tenant}/users/{id}")

    def test_add_order_does_not_change_exact_vs_catchall(self):
        catchall = Route("GET", "/files/{path...}")
        readme = Route("GET", "/files/readme")

        def hit(first: Route, second: Route) -> tuple[str, dict[str, str]]:
            router = Router()
            router.add(first, noop_handler)
            router.add(second, noop_handler)
            _, route, match = router.find_handler("", "/files/readme", "GET")
            _, _, other = router.find_handler("", "/files/docs/a.txt", "GET")
            assert dict(other.params) == {"path": "docs/a.txt"}
            return route.target, dict(match.params)

        assert hit(catchall, readme) == ("/files/readme", {})
        assert hit(readme, catchall) == ("/files/readme", {})

    def test_add_order_does_not_change_host_vs_hostless(self):
        host = Route("GET //api.example.com/api")
        hostless = Route("POST", "/api")

        def hit(first: Route, second: Route) -> tuple[str, str]:
            router = Router()
            router.add(first, noop_handler)
            router.add(second, noop_handler)
            get_route = router.find_handler("api.example.com", "/api", "GET")[1]
            post_route = router.find_handler("api.example.com", "/api", "POST")[1]
            return get_route.target, post_route.target

        assert hit(host, hostless) == ("//api.example.com/api", "/api")
        assert hit(hostless, host) == ("//api.example.com/api", "/api")

    def test_matches_catchall_params(self):
        router = Router()
        router.add(Route("GET", "/files/{path...}"), noop_handler)

        _, route, hit = router.find_handler("", "/files/docs/readme.txt", "GET")

        assert route.target == "/files/{path...}"
        assert dict(hit.params) == {"path": "docs/readme.txt"}

    def test_method_not_allowed_allowed_methods_sorted_in_allow_header(self):
        router = Router()
        router.add(Route("GET", "/r"), noop_handler)
        router.add(Route("POST", "/r"), noop_handler)
        router.add(Route("PATCH", "/r"), noop_handler)

        handler, _, _ = router.find_handler("", "/r", "DELETE")

        w = DummyWriter()
        run_handler(handler, "/r", method="DELETE", writer=w)
        assert w.headers.get("Allow") == "GET, HEAD, PATCH, POST"

    def test_literal_http_verb_segment_is_not_false_405(self):
        router = Router()
        router.add(Route("GET", "/foo/POST"), noop_handler)

        handler, route, hit = router.find_handler("", "/foo", "GET")

        assert handler is default_not_found
        assert route is EMPTY_ROUTE
        assert hit is EMPTY_MATCH
        assert hit is Match.empty()
        assert hit.pattern == ""

    def test_matches_host_pattern_case_insensitively(self):
        router = Router()
        router.add(Route("GET //API.Example.Com/users"), noop_handler)

        _, route, hit = router.find_handler("api.example.com", "/users", "GET")

        assert route.target == "//api.example.com/users"
        assert hit.pattern == "GET //api.example.com/users"

    def test_matches_host_wildcard_and_catchall(self):
        router = Router()
        dash = Route("GET /dashboard", host="{tenant}.example.com")
        files = Route("GET /files/{path...}", host="{tenant...}.cdn.example.com")
        router.add(dash, noop_handler)
        router.add(files, noop_handler)

        _, route, hit = router.find_handler("acme.example.com", "/dashboard", "GET")
        _, nested, nested_hit = router.find_handler(
            "eu.acme.cdn.example.com", "/files/docs/a.txt", "GET"
        )

        assert route is dash
        assert dict(hit.params) == {"tenant": "acme"}
        assert nested is files
        assert dict(nested_hit.params) == {
            "tenant": "eu.acme",
            "path": "docs/a.txt",
        }

    def test_escaped_host_braces_register_and_match(self):
        router = Router()
        curly = Route("GET /", host="{{API}}.example.com")
        router.add(curly, noop_handler)

        _, route, hit = router.find_handler("{api}.example.com", "/", "GET")

        assert route is curly
        assert hit.pattern == "GET //{api}.example.com/"

    def test_hostless_routes_fallback_when_no_host_branch_matches(self):
        router = Router()
        router.add(Route("GET", "/health"), noop_handler)
        router.add(Route("GET //api.example.com/users"), noop_handler)

        _, route, _ = router.find_handler("www.example.org", "/health", "GET")

        assert route.target == "/health"

    def test_rejects_duplicate_route_registration(self):
        router = Router()
        router.add(Route("GET", "/hello"), noop_handler)

        with pytest.raises(StarioError, match="Route already registered"):
            router.add(Route("GET", "/hello"), noop_handler)

    def test_rejects_prefix_registration(self):
        router = Router()

        with pytest.raises(StarioError, match="prefix route"):
            router.add(Route("/api"), noop_handler)

    def test_add_registers_route_method_and_path(self):
        send = Route("POST", "/rooms/{room_id}/send")
        router = Router()
        router.add(send, noop_handler)

        _, route, hit = router.find_handler("", "/rooms/7/send", "POST")

        assert route.target == "/rooms/{room_id}/send"
        assert dict(hit.params) == {"room_id": "7"}
        assert hit.pattern == send.pattern

        handler, _, _ = router.find_handler("", "/rooms/7/send", "GET")
        assert handler is method_not_allowed_handler(frozenset({"POST"}))

    def test_add_registers_more_specific_route(self):
        users = Route("GET", "/t/{tenant}/users/{id}")
        acme_users = Route("GET", "/t/acme/users/{id}")
        router = Router()
        router.add(acme_users, noop_handler)
        router.add(users, noop_handler)

        _, acme, acme_hit = router.find_handler("", "/t/acme/users/1", "GET")
        _, other, other_hit = router.find_handler("", "/t/beta/users/1", "GET")

        assert acme.target == "/t/acme/users/{id}"
        assert dict(acme_hit.params) == {"id": "1"}
        assert other.target == "/t/{tenant}/users/{id}"
        assert dict(other_hit.params) == {"tenant": "beta", "id": "1"}

    def test_query_registers_rfc_query_method(self):
        router = Router()
        router.add(Route("QUERY", "/feed"), noop_handler)
        router.add(Route("QUERY", "/search"), noop_handler)

        _, feed, _ = router.find_handler("", "/feed", "QUERY")
        _, search, _ = router.find_handler("", "/search", "QUERY")

        assert feed.target == "/feed"
        assert search.target == "/search"

    def test_hostless_method_wins_over_host_405(self):
        router = Router()
        router.add(Route("GET //api.example.com/api"), noop_handler)
        router.add(Route("POST", "/api"), noop_handler)

        handler, route, _ = router.find_handler("api.example.com", "/api", "POST")

        assert route.target == "/api"
        assert handler is not method_not_allowed_handler(frozenset({"GET"}))

    def test_host_405_when_only_host_tree_knows_path(self):
        router = Router()
        router.add(Route("GET //api.example.com/api"), noop_handler)

        handler, route, _ = router.find_handler("api.example.com", "/api", "POST")

        assert handler is method_not_allowed_handler(frozenset({"GET", "HEAD"}))
        assert route is EMPTY_ROUTE


class TestRouterUse:
    def test_use_applies_middleware_to_later_routes(self):
        calls: list[str] = []
        router = Router()
        router.use("/", track(calls, "scope"))
        router.add(Route("GET", "/users"), noop_handler)

        handler, _, _ = router.find_handler("", "/users", "GET")
        run_handler(handler, "/users")

        assert calls == ["scope"]

    def test_use_after_routes_applies_to_them(self):
        calls: list[str] = []
        router = Router()
        router.add(Route("GET", "/users"), noop_handler)
        router.use("/users", track(calls, "late"))

        handler, _, _ = router.find_handler("", "/users", "GET")
        run_handler(handler, "/users")

        assert calls == ["late"]

    def test_use_middleware_runs_in_registration_order(self):
        calls: list[str] = []
        router = Router()
        router.use("/", track(calls, "mw1"), track(calls, "mw2"))
        router.add(Route("GET", "/"), noop_handler)
        handler, _, _ = router.find_handler("", "/", "GET")
        run_handler(handler, "/")
        assert calls == ["mw1", "mw2"]

    def test_nested_use_scopes_run_general_to_specific(self):
        calls: list[str] = []
        router = Router()
        router.use("/", track(calls, "root"))
        router.use("/users", track(calls, "users"))
        router.add(Route("GET", "/users/panel"), noop_handler)
        handler, _, _ = router.find_handler("", "/users/panel", "GET")
        run_handler(handler, "/users/panel")
        assert calls == ["root", "users"]

    def test_route_middleware_runs_after_scope_middleware(self):
        calls: list[str] = []
        router = Router()
        router.use("/", track(calls, "scope"))
        router.add(
            Route("GET", "/"),
            noop_handler,
            middleware=[track(calls, "route")],
        )
        handler, _, _ = router.find_handler("", "/", "GET")
        run_handler(handler, "/")
        assert calls == ["scope", "route"]

    def test_host_route_inherits_hostless_path_middleware(self):
        calls: list[str] = []
        router = Router()
        router.use("/", track(calls, "root"))
        router.use("/users", track(calls, "users"))
        router.add(Route("GET //api.example.com/users"), noop_handler)

        handler, _, _ = router.find_handler("api.example.com", "/users", "GET")
        run_handler(handler, "/users", host="api.example.com")
        assert calls == ["root", "users"]

    def test_custom_not_found_on_host_branch_wins_over_hostless_miss(self):
        calls: list[str] = []

        async def host_not_found(c: Context, w: Writer) -> None:
            calls.append("host-404")

        router = Router()
        router.not_found("//api.example.com/", host_not_found)
        router.add(Route("GET", "/health"), noop_handler)

        handler, route, hit = router.find_handler("api.example.com", "/missing", "GET")
        assert route is EMPTY_ROUTE
        assert hit.pattern == ""
        run_handler(handler, "/missing", host="api.example.com")
        assert calls == ["host-404"]


def test_registering_many_routes_compiles_once() -> None:
    """Each `add` is a tree insert; the trie compiles on the first lookup."""
    import time

    router = Router()
    started = time.perf_counter()
    for i in range(3000):
        router.add(Route(f"GET /api/v1/res{i}/{{id}}/sub{i % 7}"), noop_handler)
    assert router._cy_router is None
    _handler, route, match = router.find_handler("", "/api/v1/res42/7/sub0", "GET")
    assert route.path == "/api/v1/res42/{id}/sub0"
    assert match.params == {"id": "7"}
    assert router._cy_router is not None
    # 3000 routes took ~20 s when every add recompiled.
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("value", [".", ".."])
def test_href_rejects_dot_segment_parameters(value: str) -> None:
    with pytest.raises(StarioError, match="dot segment"):
        Route("GET /u/{id}/edit").href(value)
    with pytest.raises(StarioError, match="dot segment"):
        Route("GET /files/{p...}").href(p=f"a/{value}/b")
    assert Route("GET /files/{p...}").href(p="a/..b/c.") == "/files/a/..b/c."
