"""Shared redirect encoding used by responses and Datastar SSE."""

import pytest

from stario.exceptions import StarioError
from stario.http.redirect import normalized_location
from tests.helpers import make_writer_raw

SAFE_REDIRECT_TARGETS = [
    "/dashboard",
    "/dashboard with spaces?tab=team settings#profile section",
    "//api.example.com/v1/users",
]

UNSAFE_REDIRECT_TARGETS = [
    r"/\\evil",
    "/safe\r\nx-header: injected",
    "login",
    "javascript:alert(1)",
    "ftp://example.com/file",
]

UNSAFE_REDIRECT_MATCH = "control characters|backslashes|app-relative path or absolute"


class TestNormalizedLocation:
    @pytest.mark.parametrize("target", SAFE_REDIRECT_TARGETS)
    def test_accepts_safe_targets(self, target: str) -> None:
        assert normalized_location(target)

    def test_accepts_route_host_href(self) -> None:
        from stario import Route

        target = Route("GET //{tenant}.example.com/users/{user_id}").href(
            tenant="acme", user_id="42"
        )
        assert normalized_location(target) == target

    @pytest.mark.parametrize("target", UNSAFE_REDIRECT_TARGETS)
    def test_rejects_unsafe_targets(self, target: str) -> None:
        with pytest.raises(StarioError, match=UNSAFE_REDIRECT_MATCH):
            normalized_location(target)


def test_redirect_rejects_non_3xx_status() -> None:
    w, _sink, loop = make_writer_raw()
    try:
        with pytest.raises(StarioError, match="3xx"):
            w.redirect("/ok", 200)
    finally:
        loop.close()
