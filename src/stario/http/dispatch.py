"""Route table: register `Route` values on a trie and resolve `(host, path, method)`."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache, partial

from stario.exceptions import StarioError
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.route import EMPTY_ROUTE, Route
from stario.http.segment import Segment
from stario.http.writer import Writer
from stario_cython.exchange import CRouter, compile_router

type MethodNotAllowedHandler = Callable[[frozenset[str]], Handler]
type RouteMatch = tuple[Handler, Route, Match]


def require_async_handler(handler: object, *, what: str = "Handler") -> None:
    """Reject anything that is not an async callable (checked at registration)."""
    if not callable(handler):
        raise StarioError(
            f"{what} must be callable",
            context={"got": type(handler).__name__},
            help_text="Register `async def handler(c, w):`.",
        )

    fn: object = handler
    while isinstance(fn, partial):
        fn = fn.func

    if inspect.isasyncgenfunction(fn):
        raise StarioError(
            f"{what} cannot be an async generator",
            help_text="Use `async def handler(c, w):` and write to the Writer, not `yield`.",
        )
    if inspect.iscoroutinefunction(fn):
        return

    call = fn.__call__ if callable(fn) else None
    if call is not None and call is not fn:
        if inspect.isasyncgenfunction(call):
            raise StarioError(
                f"{what} cannot be an async generator",
                help_text="Use `async def __call__(self, c, w):` and write to the Writer, not `yield`.",
            )
        if inspect.iscoroutinefunction(call):
            return

    raise StarioError(
        f"{what} must be async",
        context={"got": type(handler).__name__},
        help_text="Register `async def handler(c, w):`. Sync handlers are not supported.",
    )


def _exact_nodes() -> dict[str, Node]:
    return {}


async def default_not_found(_c: Context, w: Writer) -> None:
    w.text("Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed: frozenset[str]) -> Handler:
    allow_header = ", ".join(sorted(allowed))

    async def respond(_c: Context, w: Writer) -> None:
        w.headers.set("Allow", allow_header)
        w.text("Method Not Allowed", 405)

    return respond


@dataclass(slots=True)
class Endpoint:
    handler: Handler
    route: Route
    middleware: tuple[Middleware, ...] = ()


@dataclass(slots=True)
class Node:
    not_found_handler: Handler | None = None
    not_found_custom: bool = False
    method_not_allowed_handler: MethodNotAllowedHandler | None = None
    middleware: tuple[Middleware, ...] = ()
    exact: dict[str, Node] = field(default_factory=_exact_nodes)
    wildcard_name: str | None = None
    wildcard: Node | None = None
    catchall_name: str | None = None
    catchall: Node | None = None
    endpoints: dict[str, Endpoint] | None = None


def _param_child(current: Node, segment: Segment) -> Node | None:
    if segment.kind == "catchall":
        if current.catchall is None or current.catchall_name != segment.name:
            return None
        return current.catchall
    if current.wildcard is None or current.wildcard_name != segment.name:
        return None
    return current.wildcard


def _step(current: Node, segs: tuple[Segment, ...], i: int) -> tuple[Node, int] | None:
    seg = segs[i]
    if seg.kind == "exact":
        child = current.exact.get(seg.name)
    else:
        child = _param_child(current, seg)
    if child is None:
        return None
    return child, i + 1


def _collect_middleware(
    tree: Node,
    host_segments: tuple[Segment, ...],
    path_segments: tuple[Segment, ...],
) -> list[Middleware]:
    middlewares: list[Middleware] = list(tree.middleware)
    current = tree

    def follow(segs: tuple[Segment, ...], *, halt: bool) -> bool:
        nonlocal current
        i = 0
        while i < len(segs):
            stepped = _step(current, segs, i)
            if stepped is None:
                return halt
            current, i = stepped
            middlewares.extend(current.middleware)
        return False

    if follow(host_segments[::-1], halt=True):
        return middlewares
    follow(path_segments, halt=False)
    return middlewares


def _wrap(handler: Handler, middleware: Sequence[Middleware]) -> Handler:
    wrapped = handler
    for mw in reversed(middleware):
        wrapped = mw(wrapped)
    return wrapped


def _memoized_na(
    factory: MethodNotAllowedHandler, middleware: tuple[Middleware, ...]
) -> MethodNotAllowedHandler:
    """`factory` with `middleware` applied, built once per allowed-method set.

    The compiled router calls this on every 405 under a scope that has
    middleware or a custom handler; without the cache each one rebuilt the
    wrapper chain (and re-inspected a custom handler).
    """
    built: dict[frozenset[str], Handler] = {}

    def resolve(allowed: frozenset[str]) -> Handler:
        handler = built.get(allowed)
        if handler is None:
            handler = built[allowed] = _wrap(factory(allowed), middleware)
        return handler

    return resolve


def _path_step(node: Node, kind: str, name: str) -> Node | None:
    if kind == "exact":
        return node.exact.get(name)
    if kind == "catchall":
        if node.catchall_name != name:
            return None
        return node.catchall
    if node.wildcard_name != name:
        return None
    return node.wildcard


class _Composed:
    """Router-shaped view with middleware already applied, for `compile_router`."""

    __slots__ = (
        "_has_param_hosts",
        "_host_routing",
        "_hosts_exact",
        "_hosts_param",
        "_path",
    )

    def __init__(
        self,
        path: Node,
        hosts_exact: dict[str, Node],
        hosts_param: Node,
        has_param_hosts: bool,
        host_routing: bool,
    ) -> None:
        self._path = path
        self._hosts_exact = hosts_exact
        self._hosts_param = hosts_param
        self._has_param_hosts = has_param_hosts
        self._host_routing = host_routing


def _open_param(current: Node, segment: Segment) -> Node:
    child = _param_child(current, segment)
    if child is not None:
        return child
    catchall = segment.kind == "catchall"
    taken = current.catchall_name if catchall else current.wildcard_name
    other = current.wildcard if catchall else current.catchall
    if taken is not None:
        raise StarioError(
            "Catchall parameter conflict"
            if catchall
            else "Wildcard parameter conflict",
            context={
                "existing": taken,
                "new": segment.name,
                "segment": segment.pattern,
            },
        )
    if other is not None:
        raise StarioError(
            "Ambiguous route parameter branch",
            context={
                "existing": current.wildcard_name
                if catchall
                else current.catchall_name,
                "new": segment.name,
            },
        )
    child = Node()
    if catchall:
        current.catchall_name = segment.name
        current.catchall = child
    else:
        current.wildcard_name = segment.name
        current.wildcard = child
    return child


def _descend_or_create(current: Node, segment: Segment) -> Node:
    if segment.kind != "exact":
        return _open_param(current, segment)
    child = current.exact.get(segment.name)
    if child is None:
        child = Node()
        current.exact[segment.name] = child
    return child


class Router:
    """Route table: host routes override hostless defaults when they fully match."""

    __slots__ = (
        "_cy_router",
        "_frozen",
        "_has_param_hosts",
        "_host_routing",
        "_hosts_exact",
        "_hosts_param",
        "_path",
    )

    def __init__(self) -> None:
        self._path = Node()
        self._hosts_exact: dict[str, Node] = {}
        self._hosts_param = Node()
        self._has_param_hosts = False
        self._host_routing = False
        self._frozen = False
        # Compiled on the first lookup after a change. Registering N routes
        # is then N tree inserts and one compile, not N compiles.
        self._cy_router: CRouter | None = None

    @property
    def host_routing(self) -> bool:
        return self._host_routing

    def find_handler(self, host: str, path: str, method: str) -> RouteMatch:
        """Resolve `(host, path, method)`.

        `host` must already be lowercased (`Request.host`). `path` is the
        request path as sent (`Request.raw_path`, still percent-encoded): it is
        split on `/` first and each segment is decoded on its own, so `%2F`
        never adds a segment. Plain paths like `/users/42` are the same either
        way.
        """
        return self.compiled().lookup(host, path, method)

    def compiled(self) -> CRouter:
        """The compiled trie, built now if a registration changed it."""
        compiled = self._cy_router
        if compiled is None:
            compiled = compile_router(self._composed())
            self._cy_router = compiled
            state = getattr(self, "_app_state", None)
            if state is not None:
                state.host_routing = self._host_routing
                state.router = compiled
        return compiled

    def freeze(self) -> None:
        """Seal the table: `add`/`use`/`not_found`/`method_not_allowed` raise.

        Called once startup completes so a request-time registration bug fails
        loudly instead of silently replacing the compiled trie under traffic.
        """
        self._frozen = True

    def _check_frozen(self) -> None:
        if self._frozen:
            raise StarioError(
                "Routes are frozen after startup",
                help_text="Register routes in bootstrap before yield.",
            )

    def _invalidate_lookup(self) -> None:
        self._cy_router = None
        state = getattr(self, "_app_state", None)
        if state is not None:
            state.host_routing = self._host_routing
            state.router = None

    def _composed(self) -> _Composed:
        """Copy of the three trees with middleware folded into every handler."""
        return _Composed(
            self._compose_node(self._path, self._path, param_host=False),
            {
                host: self._compose_node(
                    tree,
                    tree,
                    param_host=False,
                    path_node=self._path,
                    path_chain=self._path.middleware,
                )
                for host, tree in self._hosts_exact.items()
            },
            self._compose_node(self._hosts_param, self._hosts_param, param_host=True),
            self._has_param_hosts,
            self._host_routing,
        )

    def _compose_node(
        self,
        node: Node,
        orig_tree: Node,
        *,
        param_host: bool,
        chain: tuple[Middleware, ...] = (),
        not_found: Handler | None = None,
        not_found_custom: bool = False,
        method_na: MethodNotAllowedHandler | None = None,
        path_node: Node | None = None,
        path_chain: tuple[Middleware, ...] = (),
    ) -> Node:
        """One new `Node` with this node's scope middleware applied.

        `chain` is this tree's middleware root→node; `not_found`/`method_na`
        are the nearest registered custom handlers in scope. `path_node` /
        `path_chain` track the hostless `_path` tree along the same segments
        for `_hosts_exact` trees (`None` elsewhere).
        """
        chain = (*chain, *node.middleware)
        if node.not_found_handler is not None:
            not_found = node.not_found_handler
            not_found_custom = node.not_found_custom
        if node.method_not_allowed_handler is not None:
            method_na = node.method_not_allowed_handler
        scoped = (*chain, *path_chain)

        out = Node(not_found_custom=not_found_custom)
        if scoped or not_found is not None:
            out.not_found_handler = _wrap(not_found or default_not_found, scoped)
        if method_na is not None and not scoped:
            out.method_not_allowed_handler = method_na
        elif scoped or method_na is not None:
            out.method_not_allowed_handler = _memoized_na(
                method_na or method_not_allowed_handler, scoped
            )
        if node.endpoints:
            out.endpoints = {}
            for method, endpoint in node.endpoints.items():
                endpoint_mws = self._endpoint_scope(
                    endpoint.route, orig_tree, param_host
                )
                wrapped = _wrap(endpoint.handler, (*endpoint_mws, *endpoint.middleware))
                if wrapped is not endpoint.handler:
                    require_async_handler(wrapped, what="Composed handler")
                out.endpoints[method] = Endpoint(
                    handler=wrapped,
                    route=endpoint.route,
                    middleware=endpoint.middleware,
                )
            # RFC 9110 9.3.2: a GET route answers HEAD too. The exchange drops
            # the body; an explicit HEAD route wins.
            get = out.endpoints.get("GET")
            if get is not None and "HEAD" not in out.endpoints:
                out.endpoints["HEAD"] = get

        def child_args(
            kind: str, name: str
        ) -> tuple[Node | None, tuple[Middleware, ...]]:
            if path_node is None:
                return None, path_chain
            pchild = _path_step(path_node, kind, name)
            if pchild is None:
                return None, path_chain
            return pchild, (*path_chain, *pchild.middleware)

        for name, child in node.exact.items():
            pnode, pchain = child_args("exact", name)
            out.exact[name] = self._compose_node(
                child,
                orig_tree,
                param_host=param_host,
                chain=chain,
                not_found=not_found,
                not_found_custom=not_found_custom,
                method_na=method_na,
                path_node=pnode,
                path_chain=pchain,
            )
        for kind, cname, cnode in (
            ("wildcard", node.wildcard_name, node.wildcard),
            ("catchall", node.catchall_name, node.catchall),
        ):
            if cnode is None or cname is None:
                continue
            pnode, pchain = child_args(kind, cname)
            composed = self._compose_node(
                cnode,
                orig_tree,
                param_host=param_host,
                chain=chain,
                not_found=not_found,
                not_found_custom=not_found_custom,
                method_na=method_na,
                path_node=pnode,
                path_chain=pchain,
            )
            if kind == "wildcard":
                out.wildcard_name = cname
                out.wildcard = composed
            else:
                out.catchall_name = cname
                out.catchall = composed
        return out

    def _endpoint_scope(
        self, route: Route, orig_tree: Node, param_host: bool
    ) -> list[Middleware]:
        walk_host = route.host_segments if param_host else ()
        scoped = _collect_middleware(orig_tree, walk_host, route.path_segments)
        if route.host:
            scoped += _collect_middleware(self._path, (), route.path_segments)
        return scoped

    def _tree_for(self, route: Route) -> Node:
        if not route.host:
            return self._path
        self._host_routing = True
        if all(segment.kind == "exact" for segment in route.host_segments):
            return self._hosts_exact.setdefault(route.host, Node())
        self._has_param_hosts = True
        return self._hosts_param

    def _descend(self, tree: Node, route: Route) -> Node:
        current = tree
        if tree is self._hosts_param:
            for segment in route.host_segments[::-1]:
                current = _descend_or_create(current, segment)
        for segment in route.path_segments:
            current = _descend_or_create(current, segment)
        return current

    def _policy_node(self, pattern: str) -> Node:
        route = Route(pattern)
        if any(segment.kind == "catchall" for segment in route.host_segments):
            raise StarioError(
                "Catchall host policy is not supported",
                context={"target": route.target},
            )
        if route.path_segments and route.path_segments[-1].kind == "catchall":
            raise StarioError(
                "Catchall route policy cannot have child routes",
                context={"target": route.target},
            )
        tree = self._tree_for(route)
        return self._descend(tree, route)

    def use(self, pattern: str, *middleware: Middleware) -> None:
        self._check_frozen()
        current = self._policy_node(pattern)
        if not middleware:
            return
        current.middleware = current.middleware + tuple(middleware)
        self._invalidate_lookup()

    def not_found(self, pattern: str, handler: Handler) -> None:
        self._check_frozen()
        require_async_handler(handler, what="Not-found handler")
        node = self._policy_node(pattern)
        node.not_found_handler = handler
        node.not_found_custom = True
        self._invalidate_lookup()

    def method_not_allowed(
        self,
        pattern: str,
        handler: MethodNotAllowedHandler,
    ) -> None:
        self._check_frozen()

        def checked(allowed: frozenset[str]) -> Handler:
            resolved = handler(allowed)
            require_async_handler(resolved, what="Method-not-allowed handler")
            return resolved

        self._policy_node(pattern).method_not_allowed_handler = _memoized_na(
            checked, ()
        )
        self._invalidate_lookup()

    def add(
        self,
        route: Route,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self._check_frozen()
        require_async_handler(handler)
        if route is EMPTY_ROUTE:
            raise StarioError("Cannot register the unmatched route")
        if not route.method:
            raise StarioError(
                "Cannot register a prefix route",
                context={"target": route.target},
            )
        method = route.method
        tree = self._tree_for(route)
        current = self._descend(tree, route)

        existing = None if current.endpoints is None else current.endpoints.get(method)
        if existing is not None:
            raise StarioError(
                "Route already registered",
                context={"method": method, "target": route.target},
            )

        endpoint = Endpoint(handler=handler, route=route, middleware=tuple(middleware))
        if current.endpoints is None:
            current.endpoints = {method: endpoint}
        else:
            current.endpoints[method] = endpoint
        self._invalidate_lookup()


__all__ = [
    "Router",
    "default_not_found",
    "method_not_allowed_handler",
]
