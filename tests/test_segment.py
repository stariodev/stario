"""Tests for route pattern segment parsing."""

import pytest

from stario.exceptions import StarioError
from stario.http.segment import (
    Segment,
    host_pattern_labels,
    parse_host_segments,
    parse_path_segments,
    parse_route,
)


class TestSegmentParse:
    def test_exact_segment(self):
        segment = Segment.parse("/users", "users")

        assert segment.kind == "exact"
        assert segment.name == "users"
        assert segment.pattern == "users"

    def test_wildcard_segment(self):
        segment = Segment.parse("/users/{id}", "{id}")

        assert segment.kind == "wildcard"
        assert segment.name == "id"
        assert segment.pattern == "{id}"

    def test_catchall_segment(self):
        segment = Segment.parse("/files/{path...}", "{path...}")

        assert segment.kind == "catchall"
        assert segment.name == "path"
        assert segment.pattern == "{path...}"

    def test_rejects_partial_placeholder(self):
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "{id}-edit")
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "pre{id}")
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/files", "{path...}.txt")
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/users", "{id}{other}")

    def test_rejects_unclosed_placeholder(self):
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "{broken")
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "id}")

    def test_rejects_empty_parameter_name(self):
        with pytest.raises(StarioError, match="parameter name is empty"):
            Segment.parse("/items", "{}")

    @pytest.mark.parametrize("name", ["query", "fragment"])
    def test_rejects_reserved_href_names(self, name: str):
        with pytest.raises(StarioError, match="reserved"):
            Segment.parse(f"/{{{name}}}", f"{{{name}}}")

    @pytest.mark.parametrize("name", ["class", "for", "def", "return", "async"])
    def test_rejects_python_keyword_names(self, name: str):
        with pytest.raises(StarioError, match="Python keyword"):
            Segment.parse(f"/{{{name}}}", f"{{{name}}}")

    def test_rejects_format_spec_and_conversion(self):
        with pytest.raises(StarioError, match="format specs or conversions"):
            Segment.parse("/users/{id:d}", "{id:d}")
        with pytest.raises(StarioError, match="format specs or conversions"):
            Segment.parse("/users/{id!s}", "{id!s}")

    def test_is_frozen(self):
        segment = Segment.parse("/users", "users")

        with pytest.raises(AttributeError, match="immutable"):
            segment.name = "other"


class TestHostPatternLabels:
    def test_splits_simple_host(self):
        assert host_pattern_labels("api.example.com") == [
            "api",
            "example",
            "com",
        ]

    def test_preserves_catchall_label(self):
        assert host_pattern_labels("{tenant...}.example.com") == [
            "{tenant...}",
            "example",
            "com",
        ]


class TestParseHostSegments:
    def test_lowercases_exact_labels(self):
        segments = parse_host_segments("API.Example.COM")

        assert [segment.name for segment in segments] == ["api", "example", "com"]

    def test_unescapes_then_lowers_exact_braces(self):
        segments = parse_host_segments("{{API}}.Example.COM")

        assert segments[0].kind == "exact"
        assert segments[0].name == "{api}"
        assert [segment.pattern for segment in segments] == [
            "{api}",
            "example",
            "com",
        ]

    def test_parses_wildcard_and_catchall_labels(self):
        wild = parse_host_segments("{tenant}.example.com")
        rest = parse_host_segments("{tenant...}.example.com")

        assert wild[0].kind == "wildcard"
        assert wild[0].name == "tenant"
        assert rest[0].kind == "catchall"
        assert rest[0].name == "tenant"
        assert [segment.kind for segment in rest[1:]] == ["exact", "exact"]

    def test_rejects_partial_placeholder_label(self):
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            parse_host_segments("api-{tenant}.example.com")
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            parse_host_segments("{tenant...}extra.example.com")

    def test_rejects_empty_label(self):
        with pytest.raises(StarioError, match="empty host label"):
            parse_host_segments("api..example.com")

    def test_rejects_catchall_after_first_label(self):
        with pytest.raises(
            StarioError, match="Catchall host param in invalid position"
        ):
            parse_host_segments("example.{tenant...}.com")


class TestParsePathSegments:
    def test_parses_canonical_path(self):
        segments = parse_path_segments("/users/{user_id}")

        assert len(segments) == 2
        assert segments[0].kind == "exact"
        assert segments[1].name == "user_id"

    def test_root_has_no_segments(self):
        assert parse_path_segments("/") == ()

    def test_rejects_empty_segment(self):
        with pytest.raises(StarioError, match="empty path segment"):
            parse_path_segments("/users//profile")

    def test_unescapes_doubled_braces_as_exact(self):
        segments = parse_path_segments("/curly/{{id}}")

        assert segments[1].kind == "exact"
        assert segments[1].name == "{id}"

    def test_parse_route_rejects_duplicate_names(self):
        with pytest.raises(StarioError, match="Duplicate route parameter"):
            parse_route(None, "/teams/{id}/users/{id}")
        with pytest.raises(StarioError, match="Duplicate route parameter"):
            parse_route("{id}.example.com", "/users/{id}")

    def test_rejects_catchall_before_last_segment(self):
        with pytest.raises(
            StarioError, match="Catchall path param in invalid position"
        ):
            parse_path_segments("/files/{path...}/download")
