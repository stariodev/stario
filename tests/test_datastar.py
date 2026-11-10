"""Tests for stario.datastar module - SSE events, attributes, and signals."""

import json
from typing import Any, cast

import pytest

from stario import Route
from stario.datastar import (
    DATASTAR_CDN_URL,
    SSE,
    ModuleScript,
    at,
    data,
)
from stario.datastar.attributes import DatastarAttributes
from stario.datastar.format import (
    check_signal_name,
    debounce_to_string,
    expand_signal_paths,
    js_object,
    signal_attr_key,
    signal_object,
    string_literal,
    throttle_to_string,
    time_to_string,
)
from stario.exceptions import StarioError, StarioRuntime
from stario.markup import html as h
from stario.markup import render
from stario.markup.escape import escape_attribute_value, escape_sq_attribute_value
from stario.markup.types import Attrs
from tests.helpers import TestWriter


class _ClosedLoop:
    def close(self) -> None:
        return None


def _make_writer() -> tuple[TestWriter, bytearray, _ClosedLoop]:
    writer = TestWriter()
    return writer, writer.buf, _ClosedLoop()


def _sse_body(writer: TestWriter) -> bytes:
    return writer.body


Div = h.Div


def _wire_attrs(mapping: dict[str, str | bool]) -> Attrs:
    parts: list[str] = []
    for key, value in mapping.items():
        if value is True:
            parts.append(f" {key}")
        elif isinstance(value, str) and "signals" in key and '"' in value:
            parts.append(f" {key}='{escape_sq_attribute_value(value)}'")
        elif isinstance(value, str) and '"' in value:
            parts.append(f' {key}="{escape_attribute_value(value)}"')
        else:
            parts.append(f' {key}="{value}"')
    return Attrs("".join(parts))


class TestSseSignals:
    """Test writer-bound signal patching helpers."""

    def test_patch_signals_accepts_any_signal_key(self):
        """The JSON payload carries any key verbatim — no name conversion."""
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"myCount": 1, "my-name": 2, "_x": 3})
            assert b'data: signals {"myCount":1,"my-name":2,"_x":3}' in _sse_body(w)
        finally:
            loop.close()

    def test_patch_signals_rejects_completed_writer(self):
        w, _sink, loop = _make_writer()
        try:
            w.end()
            with pytest.raises(StarioRuntime, match="after the response is completed"):
                SSE(w).patch_signals({"count": 1})
        finally:
            loop.close()


class TestSseNavigate:
    """Test client-side navigation over SSE."""

    def test_navigate_with_special_chars(self):
        """URL is embedded as a JSON string literal; single quotes survive as-is."""
        w, _sink, loop = _make_writer()
        try:
            SSE(w).navigate("/page?name=O'Brien")
            result = _sse_body(w)

            assert b'window.location = "/page?name=O\'Brien"' in result
        finally:
            loop.close()

    def test_navigate_with_unicode(self):
        """Non-ASCII path segments are percent-encoded before embedding."""
        w, _sink, loop = _make_writer()
        try:
            SSE(w).navigate("/users/日本語")
            result = _sse_body(w)

            assert b'window.location = "/users/%E6%97%A5%E6%9C%AC%E8%AA%9E"' in result
        finally:
            loop.close()

    def test_navigate_percent_encodes_script_breakout(self):
        """`</script>` in a redirect target cannot break out of the script patch."""
        w, _sink, loop = _make_writer()
        try:
            SSE(w).navigate("/page?q=</script><script>alert(1)</script>")
            result = _sse_body(w)

            # The angle brackets must be percent-encoded inside the JS string;
            # the only raw </script> on the wire is the patch's own closing tag.
            assert b"%3C/script%3E" in result
            assert result.count(b"</script>") == 1
            assert b"<script>alert(1)</script>" not in result
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "JaVaScRiPt:alert(1)",
        ],
    )
    def test_navigate_rejects_forbidden_schemes(self, url: str):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="app-relative path or absolute"):
                SSE(w).navigate(url)
            assert w.body == b""
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "url",
        [
            "/safe\r\nx: injected",
            r"/\evil",
        ],
    )
    def test_navigate_rejects_crlf_and_unsafe_paths(self, url: str):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError):
                SSE(w).navigate(url)
            assert w.body == b""
        finally:
            loop.close()


class TestSseWireFormat:
    """Pin the exact SSE wire contract: data-line splitting, modes, encodings."""

    def test_constructor_does_not_start_response(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w)

            assert not w.started
            assert w.body == b""
        finally:
            loop.close()

    def test_open_sends_headers_before_first_event(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).open()

            assert w.headers.get("content-type") == "text/event-stream"
            assert w.headers.get("cache-control") == "no-cache"
            assert w.body == b""
        finally:
            loop.close()

    def test_first_event_opens_stream_lazily(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"ok": True})

            assert w.headers.get("content-type") == "text/event-stream"
            assert w.headers.get("cache-control") == "no-cache"
            assert b'data: signals {"ok":true}' in _sse_body(w)
        finally:
            loop.close()

    def test_patch_elements_rejects_unknown_mode(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="patch mode"):
                SSE(w).patch_elements(h.Div("x"), mode=cast(Any, "sideways"))
        finally:
            loop.close()

    def test_patch_elements_rejects_unknown_namespace(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="patch namespace"):
                SSE(w).patch_elements(h.Div("x"), namespace=cast(Any, "html"))
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "mode", ["inner", "replace", "prepend", "append", "before", "after"]
    )
    def test_non_outer_modes_emit_mode_line(self, mode):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(h.Div("x"), mode=mode, selector="#t")
            result = _sse_body(w)

            assert f"data: mode {mode}".encode() in result
        finally:
            loop.close()

    def test_omitted_mode_omits_mode_line(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(h.Div("x"))
            result = _sse_body(w)

            assert b"data: mode" not in result
        finally:
            loop.close()

    def test_mathml_namespace(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(b"<mi>x</mi>", namespace="mathml")
            result = _sse_body(w)

            assert b"data: namespace mathml" in result
        finally:
            loop.close()

    def test_multiline_html_splits_into_repeated_data_lines(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements("<div>\n  <p>a</p>\n</div>")
            result = _sse_body(w)

            assert (
                b"data: elements <div>\n"
                b"data: elements   <p>a</p>\n"
                b"data: elements </div>\n\n"
            ) in result
        finally:
            loop.close()

    def test_crlf_in_patch_elements_splits_once(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements("<div>\r\n<p>a</p>\r\n</div>")
            result = _sse_body(w)

            assert (
                b"data: elements <div>\n"
                b"data: elements <p>a</p>\n"
                b"data: elements </div>\n\n"
            ) in result
            assert b"\r" not in result
        finally:
            loop.close()

    def test_bare_cr_in_patch_elements_becomes_data_line(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements("<div>\revil")
            result = _sse_body(w)
            assert b"event: datastar-patch-elements\n" in result
            assert b"\revil" not in result
            assert b"data: elements <div>\n" in result
            assert b"data: elements evil" in result
            assert b"event: evil" not in result
        finally:
            loop.close()

    def test_patch_signals_rejects_raw_json_text(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(TypeError, match="mapping"):
                SSE(w).patch_signals('{"raw":true}')  # type: ignore[arg-type]
            assert w.body == b""
        finally:
            loop.close()

    def test_unicode_signals_are_utf8_on_the_wire(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"msg": "日本語"})
            result = _sse_body(w)

            assert 'data: signals {"msg":"日本語"}'.encode() in result
        finally:
            loop.close()

    def test_patch_signals_rejects_raw_json_bytes(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(TypeError, match="mapping"):
                SSE(w).patch_signals(b'{"raw":true}')  # type: ignore[arg-type]
            assert w.body == b""
        finally:
            loop.close()


class TestSseScriptTrustContract:
    """`execute_script()` streams developer-authored JS verbatim.

    The code is intentionally NOT escaped: it is a trusted-content API.
    Untrusted input must never be interpolated into `execute_script()` —
    a `</script>` sequence would break out of the script element.
    """

    def test_execute_streams_code_verbatim_including_script_close(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).execute_script('console.log("</script>")', auto_remove=False)
            result = _sse_body(w)

            assert b'data: elements <script>console.log("</script>")</script>' in result
        finally:
            loop.close()

    def test_multiline_code_splits_into_data_lines(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).execute_script(
                "let a = 1;\nconsole.log(a);",
                auto_remove=False,
            )
            result = _sse_body(w)

            assert (
                b"data: elements <script>let a = 1;\n"
                b"data: elements console.log(a);</script>"
            ) in result
        finally:
            loop.close()


class TestDatastarThroughRender:
    """Datastar attribute payloads must survive HTML attribute escaping."""

    def test_signals_json_round_trips_through_attribute_escaping(self):
        import html as html_mod

        payload = {"q": 'He said "hi" & left', "n": 1}
        out = render(h.Div(data.signals(payload)))

        prefix = "<div data-signals='"
        assert out.startswith(prefix)
        value = out.split(prefix, 1)[1].split("'", 1)[0]
        assert "'" not in value  # attribute delimiter cannot be terminated early
        assert json.loads(html_mod.unescape(value)) == payload


class TestSseRemove:
    """Test remove helper."""

    def test_remove_rejects_line_breaks_in_selector(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="line breaks"):
                SSE(w).remove("#old\ndata: mode append")
            assert w.body == b""
        finally:
            loop.close()


class TestSseEventMeta:
    """`id:`/`retry:` fields and the `ping()` comment on the wire."""

    def test_ping_writes_comment_and_opens_stream(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).ping("beat")
            assert _sse_body(w) == b": beat\n\n"
            assert w.headers.get("content-type") == "text/event-stream"
        finally:
            loop.close()

    def test_ping_rejects_line_breaks(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="line breaks"):
                SSE(w).ping("a\nb")
        finally:
            loop.close()

    def test_patch_elements_emits_id_and_retry_after_event_line(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(b"<p>x</p>", event_id="e7", retry=250)
            body = _sse_body(w)
            assert body.startswith(
                b"event: datastar-patch-elements\nid: e7\nretry: 250\n"
            )
        finally:
            loop.close()

    def test_retry_default_1000_is_omitted(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(b"<p>x</p>", retry=1000)
            assert b"retry:" not in _sse_body(w)
        finally:
            loop.close()

    @pytest.mark.parametrize("retry", [0, -1, True, "500", 2.5])
    def test_retry_must_be_positive_int(self, retry):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="positive int"):
                SSE(w).patch_elements(b"<p>x</p>", retry=cast(Any, retry))
        finally:
            loop.close()

    @pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
    def test_event_id_rejects_line_breaks_and_nul(self, bad):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError):
                SSE(w).patch_elements(b"<p>x</p>", event_id=bad)
        finally:
            loop.close()

    def test_patch_signals_and_remove_emit_meta(self):
        w, _sink, loop = _make_writer()
        try:
            sse = SSE(w)
            sse.patch_signals({"n": 1}, event_id="s1", retry=50)
            sse.remove("#x", event_id="r1")
            body = _sse_body(w)
            assert b"event: datastar-patch-signals\nid: s1\nretry: 50\n" in body
            assert b"event: datastar-patch-elements\nid: r1\n" in body
        finally:
            loop.close()

    def test_navigate_and_execute_script_pass_meta_through(self):
        w, _sink, loop = _make_writer()
        try:
            sse = SSE(w)
            sse.execute_script("console.log(1)", event_id="js1")
            sse.navigate("/next", retry=42)
            body = _sse_body(w)
            assert body.count(b"id: js1\n") == 1
            assert body.count(b"retry: 42\n") == 1
        finally:
            loop.close()


class TestDatastarAttributeValidation:
    """Validation rules not covered by the wire-format matrix."""

    def test_signal_names_reject_only_empty_or_broken_paths(self):
        for bad in ("", "a..b", ".a", "a.", 5, None):
            with pytest.raises(StarioError, match="Invalid Datastar signal name"):
                check_signal_name(bad)

    def test_attr_rejects_quote_breakout_key(self):
        with pytest.raises(StarioError, match="Invalid attribute name"):
            data.attr('x" onfocus="alert(1)', "$x")

    def test_on_camel_case_event_gets_case_modifier(self):
        # HTML lowercases the attribute name and Datastar's default for
        # data-on is kebab, so `myEvent` must travel as `my-event__case.camel`.
        assert data.on("myEvent", "go()") == Attrs(
            ' data-on:my-event__case.camel="go()"'
        )
        assert data.on("DOMContentLoaded", "go()", target="window") == Attrs(
            ' data-on:d-o-m-content-loaded__window__case.pascal="go()"'
        )
        # An explicit case wins over inference.
        assert data.on("myEvent", "go()", case="snake") == Attrs(
            ' data-on:my-event__case.snake="go()"'
        )

    def test_on_rejects_name_no_case_modifier_can_restore(self):
        # `my-event-x` camel-cases to `myEventX`, so no modifier restores it.
        with pytest.raises(StarioError, match="cannot be spelled"):
            data.on("myEvent-x", "go()")

    def test_class_camel_case_name_gets_case_modifier(self):
        assert data.class_("myClass", "$x") == Attrs(
            ' data-class:my-class__case.camel="$x"'
        )
        assert data.class_("text-red-500", "$x") == Attrs(
            ' data-class:text-red-500="$x"'
        )
        assert data.class_("my_class", "$x") == Attrs(' data-class:my_class="$x"')

    def test_attr_and_style_reject_uppercase_keys(self):
        with pytest.raises(StarioError, match="uppercase"):
            data.attr("viewBox", "$box")
        with pytest.raises(StarioError, match="uppercase"):
            data.style("--myVar", "$v")
        # The value form keeps the case as a JavaScript object key.
        assert data.attrs({"viewBox": "$box"}) == Attrs(
            " data-attr=\"{'viewBox':$box}\""
        )

    def test_bind_rejects_uppercase_modifier_values(self):
        with pytest.raises(StarioError, match="uppercase"):
            data.bind("html", prop="innerHTML")
        with pytest.raises(StarioError, match="uppercase"):
            data.bind("x", event="myChange")

    def test_on_rejects_breakout_event_name(self):
        with pytest.raises(StarioError, match="Invalid attribute name"):
            data.on('click" onfocus="alert(1)', "go()")

    @pytest.mark.parametrize(
        "time",
        [None, True, -1, -0.1, float("inf"), "0.5s", "150", "fast", "1.2ms"],
    )
    def test_time_to_string_rejects_ambiguous_values(self, time):
        with pytest.raises(StarioError, match="Invalid Datastar time value"):
            time_to_string(time)

    def test_timing_modifier_helpers_reject_unknown_modifiers(self):
        with pytest.raises(StarioError, match="Invalid debounce modifier"):
            debounce_to_string(("150ms", "trailing"))  # type: ignore[arg-type]

        with pytest.raises(StarioError, match="Invalid throttle modifier"):
            throttle_to_string(("150ms", "leading"))  # type: ignore[arg-type]


class TestStringLiteral:
    """`<` escapes as `\\x3c` so user text cannot close a script or attribute."""

    def test_escapes_script_close(self):
        assert string_literal("</script>") == "'\\x3c/script>'"

    def test_output_contains_no_raw_lt(self):
        out = string_literal("</script><script>alert(1)</script>")
        assert "<" not in out


class TestSignalNames:
    """Signal names pass through to Datastar unchanged; `_` means local."""

    @pytest.mark.parametrize(
        ("actual", "expected"),
        [
            # bind: the value form uses the name verbatim
            (data.bind("myCount"), Attrs(' data-bind="myCount"')),
            (data.bind("my_count"), Attrs(' data-bind="my_count"')),
            (data.bind("my-count"), Attrs(' data-bind="my-count"')),
            (data.bind("MyCount"), Attrs(' data-bind="MyCount"')),
            (data.bind("_local"), Attrs(' data-bind="_local"')),
            (data.bind("ui.open"), Attrs(' data-bind="ui.open"')),
            (data.bind("ui._open"), Attrs(' data-bind="ui._open"')),
            (
                data.bind("myCount", prop="checked", event="change"),
                Attrs(' data-bind__prop.checked__event.change="myCount"'),
            ),
            (
                data.bind("ui._open", prop="checked"),
                Attrs(' data-bind__prop.checked="ui._open"'),
            ),
            (
                data.bind("query", event="input"),
                Attrs(' data-bind__event.input="query"'),
            ),
            # indicator and ref: value form
            (data.indicator("myCount"), Attrs(' data-indicator="myCount"')),
            (data.indicator("_saving"), Attrs(' data-indicator="_saving"')),
            (data.ref("searchInput"), Attrs(' data-ref="searchInput"')),
            (data.ref("ui.el"), Attrs(' data-ref="ui.el"')),
            # signal: value-form object; dotted names nest
            (
                data.signal("myCount", "0"),
                Attrs(" data-signals=\"{'myCount': (0)}\""),
            ),
            (
                data.signal("_local", "0"),
                Attrs(" data-signals=\"{'_local': (0)}\""),
            ),
            (
                data.signal("ui.open", "0"),
                Attrs(" data-signals=\"{'ui': {'open': (0)}}\""),
            ),
            (
                data.signal("myCount", "$a, $b"),
                Attrs(" data-signals=\"{'myCount': ($a, $b)}\""),
            ),
            (
                data.signal("myCount", "0", if_missing=True),
                Attrs(" data-signals__ifmissing=\"{'myCount': (0)}\""),
            ),
            # signals(): JSON form, keys verbatim
            (
                data.signals({"_menu_open": False}),
                Attrs(""" data-signals='{"_menu_open":false}'"""),
            ),
            # computed: value form, arrow function per entry (>&gt; escaped on the wire)
            (
                data.computed("fullName", "$first + $last"),
                Attrs(" data-computed=\"{'fullName':() =&gt; ($first + $last)}\""),
            ),
            (
                data.computed("ui.label", "$x"),
                Attrs(" data-computed=\"{'ui':{'label':() =&gt; ($x)}}\""),
            ),
            (
                data.computeds({"a": "$x", "b.c": "$y", "b.d": "$z"}),
                Attrs(
                    " data-computed=\"{'a':() =&gt; ($x),'b':{'c':() =&gt; ($y),'d':() =&gt; ($z)}}\""
                ),
            ),
            # match_media: key form spelled to round-trip through case conversion
            (
                data.match_media("isDark", "$dark"),
                Attrs(' data-match-media:is-dark="$dark"'),
            ),
            (
                data.match_media("is_dark", "$dark"),
                Attrs(' data-match-media:is_dark__case.kebab="$dark"'),
            ),
            (
                data.match_media("IsDark", "$dark"),
                Attrs(' data-match-media:is-dark__case.pascal="$dark"'),
            ),
            (
                data.match_media("_menu_open", "$dark"),
                Attrs(' data-match-media:_menu_open__case.kebab="$dark"'),
            ),
        ],
    )
    def test_signal_names_render_verbatim(self, actual, expected):
        assert actual == expected

    def test_computeds_rejects_leaf_and_prefix_conflict(self):
        with pytest.raises(StarioError, match="Conflicting Datastar signal paths"):
            data.computeds({"a.b": "$x", "a": "$y"})
        with pytest.raises(StarioError, match="Conflicting Datastar signal paths"):
            data.computeds({"a": "$y", "a.b": "$x"})

    def test_signal_object_wraps_value_in_parentheses(self):
        assert signal_object("ui._open", "0") == "{'ui': {'_open': (0)}}"
        assert signal_object("myCount", "$a, $b") == "{'myCount': ($a, $b)}"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("isDark", "is-dark"),
            ("is_dark", "is_dark__case.kebab"),
            ("IsDark", "is-dark__case.pascal"),
            ("_x", "_x__case.kebab"),
            ("my-count", "my-count__case.kebab"),
            ("dark2", "dark2__case.kebab"),
        ],
    )
    def test_signal_attr_key_round_trips(self, name, expected):
        assert signal_attr_key(name) == expected

    @pytest.mark.parametrize(
        "name",
        ["a__b", "a b", "aB-c", 'a"b', "a'b", "a>b", "a/b", "a=b"],
    )
    def test_signal_attr_key_rejects_unspellable_names(self, name):
        with pytest.raises(StarioError, match="attribute key"):
            signal_attr_key(name)

    def test_patch_signals_accepts_local_signal_key(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"_draft": ""})
            assert b'data: signals {"_draft":""}' in _sse_body(w)
        finally:
            loop.close()


class TestExpandSignalPaths:
    """`{"ui.open": x}` becomes `{"ui": {"open": x}}` in every JSON payload."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"ui.open": True}, {"ui": {"open": True}}),
            ({"a.b.c": 1}, {"a": {"b": {"c": 1}}}),
            (
                {"ui": {"a": 1}, "ui.b": 2},
                {"ui": {"a": 1, "b": 2}},
            ),
            # a nested dict's own dotted keys expand the same way
            ({"a": {"b.c": 1}}, {"a": {"b": {"c": 1}}}),
            # a dict merges onto an existing dict path
            ({"a.b": 1, "a": {"c": 2}}, {"a": {"b": 1, "c": 2}}),
            # None is a leaf — Datastar uses it to remove a signal
            ({"gone": None, "ui.open": None}, {"gone": None, "ui": {"open": None}}),
            # lists and other values stay untouched leaves
            (
                {"list": [1, {"a.b": 2}], "ui.x": "y"},
                {"list": [1, {"a.b": 2}], "ui": {"x": "y"}},
            ),
        ],
    )
    def test_dotted_keys_expand(self, payload, expected):
        assert expand_signal_paths(payload) == expected

    def test_no_dot_returns_an_equal_copy(self):
        payload = {"count": 0, "ui": {"open": True, "deep": {"x": 1}}}
        expanded = expand_signal_paths(payload)
        assert expanded == payload
        assert expanded is not payload
        # the fast path does not rebuild nested values
        assert expanded["ui"] is payload["ui"]

    @pytest.mark.parametrize(
        "payload",
        [
            # a path runs through a non-dict leaf
            {"a": 1, "a.b": 2},
            {"a.b": 2, "a.b.c": 1},
            # a leaf lands on a path (same leaf path set twice)
            {"a.b": 2, "a": 1},
            {"ui": {"open": 1}, "ui.open": 2},
            {"a": {"b": 1}, "a.b": 2},
            # a dict lands on an existing leaf
            {"a": 1, "a.b": {"c": 2}},
        ],
    )
    def test_conflicting_paths_raise(self, payload):
        with pytest.raises(StarioError, match="Conflicting Datastar signal paths"):
            expand_signal_paths(payload)

    @pytest.mark.parametrize("key", ["", "a..b", ".a", "a.", 1, None])
    def test_bad_names_raise(self, key):
        with pytest.raises(StarioError, match="Invalid Datastar signal name"):
            expand_signal_paths({key: 1})

    def test_signals_attribute_expands_dotted_keys(self):
        assert data.signals({"ui.open": True, "count": 0}) == Attrs(
            ' data-signals=\'{"ui":{"open":true},"count":0}\''
        )

    def test_patch_signals_expands_dotted_keys(self):
        w, _sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"ui.open": True})
            assert b'data: signals {"ui":{"open":true}}' in _sse_body(w)
        finally:
            loop.close()


class TestDatastarAttributeMatrix:
    """Table-driven contract for every attribute helper's wire format."""

    @pytest.mark.parametrize(
        ("actual", "expected"),
        [
            (data.ignore_morph(), {"data-ignore-morph": True}),
            (
                data.computed("full_name", "$a + $b"),
                {"data-computed": "{'full_name':() =&gt; ($a + $b)}"},
            ),
            (data.signals({"count": 0}), {"data-signals": '{"count":0}'}),
            (data.bind("email"), {"data-bind": "email"}),
            (
                data.bind("is_checked", prop="checked"),
                {"data-bind__prop.checked": "is_checked"},
            ),
            (
                data.bind("query", event="input.change"),
                {"data-bind__event.input.change": "query"},
            ),
            (
                data.bind("is_checked", prop="checked", event="change"),
                {"data-bind__prop.checked__event.change": "is_checked"},
            ),
            (data.show("$visible"), {"data-show": "$visible"}),
            (
                data.on("submit", "go()", prevent=True, stop=True),
                {"data-on:submit__prevent__stop": "go()"},
            ),
            (
                data.on("click", "go()", prevent=True),
                {"data-on:click__prevent": "go()"},
            ),
            (
                data.on("keydown", "go()", debounce=("150ms", "leading")),
                {"data-on:keydown__debounce.150ms.leading": "go()"},
            ),
            (
                data.on_intersect("seen()", threshold="half"),
                {"data-on-intersect__half": "seen()"},
            ),
            (
                data.on_intersect("seen()", view_transition=True),
                {"data-on-intersect__viewtransition": "seen()"},
            ),
            (
                data.persist(include=["draft", "settings"]),
                {"data-persist": "{'include':'draft|settings'}"},
            ),
            (data.persist(session=True), {"data-persist__session": True}),
            (
                data.persist(storage_key="prefs", session=True),
                {"data-persist:prefs__session": True},
            ),
            (
                DatastarAttributes("data-star-").text("$title"),
                {"data-star-text": "$title"},
            ),
            (
                data.scroll_into_view(behavior="smooth", vertical="center", focus=True),
                {"data-scroll-into-view__smooth__vcenter__focus": True},
            ),
        ],
        ids=lambda value: next(iter(value)) if isinstance(value, dict) else "case",
    )
    def test_attribute_helper_wire_format(self, actual, expected):
        assert actual == _wire_attrs(expected)

    def test_on_rejects_passive_with_prevent(self):
        with pytest.raises(StarioError, match="passive and prevent"):
            data.on("touchstart", "go()", passive=True, prevent=True)

    @pytest.mark.parametrize("threshold", [-0.1, 1.5, 25, 100.0])
    def test_on_intersect_rejects_out_of_range_numeric_threshold(self, threshold):
        # `threshold.1.5` would misparse client-side (`.` separates modifier args).
        with pytest.raises(StarioError, match="Invalid intersection threshold"):
            data.on_intersect("load()", threshold=threshold)


class TestSignalsConversion:
    def test_signals_rejects_plain_object(self):
        """No implicit `__dict__` serialization; `vars(obj)` is the explicit spelling."""

        class State:
            def __init__(self):
                self.count = 1

        with pytest.raises(TypeError, match="mapping"):
            data.signals(State())  # type: ignore[arg-type]

    def test_signals_from_vars_of_plain_object(self):
        class State:
            def __init__(self):
                self.count = 1

        assert data.signals(vars(State())) == _wire_attrs(
            {"data-signals": '{"count":1}'}
        )

    def test_signals_rejects_pydantic_like_model(self):
        class Model:
            def model_dump(self):
                return {"name": "ada"}

        with pytest.raises(TypeError, match="mapping"):
            data.signals(Model())  # type: ignore[arg-type]

    def test_signals_rejects_unconvertible_value(self):
        with pytest.raises(TypeError, match="mapping"):
            data.signals(42)  # type: ignore[arg-type]


class TestDatastarActions:
    """Test Datastar action helpers."""

    def test_get_with_query(self):
        action = at.get("/search", {"q": "test"})
        assert action == "@get('/search?q=test')"

    def test_get_merges_existing_query(self):
        action = at.get("/search?sort=recent", {"q": "test"})
        assert action == "@get('/search?sort=recent&q=test')"

    def test_get_query_uses_doseq(self):
        action = at.get("/search", {"tag": ["a", "b"]})
        assert action == "@get('/search?tag=a&tag=b')"

    def test_post_with_options(self):
        payload = {"extra": 123}

        action = at.post(
            "/api/submit",
            include="form.*",
            retry="error",
            payload=payload,
        )

        assert "@post" in action
        assert "/api/submit" in action

        expected_payload_str = f"payload: {js_object(payload)}"
        assert expected_payload_str in action

        assert "retry: 'error'" in action

    def test_selector_requires_form_content_type(self):
        action = at.post("/checkout", content_type="form", selector="#checkout-form")
        assert "contentType: 'form'" in action
        assert "selector: '#checkout-form'" in action

        with pytest.raises(StarioError, match="content_type='form'"):
            at.post("/checkout", selector="#checkout-form")

    def test_retry_max_wait_uses_v1_option_name(self):
        """Datastar 1.0.x expects `retryMaxWait`, not `retryMaxWaitMs`."""
        action = at.get("/api", retry_max_wait_ms=5_000)
        assert "retryMaxWait: 5000" in action
        assert "retryMaxWaitMs" not in action

    def test_open_when_hidden_tristate(self):
        # Omitted options defer to the client default and are elided.
        assert at.post("/api") == "@post('/api')"
        # Explicit True/False are emitted (the client default is method-dependent).
        assert "openWhenHidden: true" in at.get("/api", open_when_hidden=True)
        assert "openWhenHidden: false" in at.post("/api", open_when_hidden=False)

    def test_none_fetch_options_are_omitted(self):
        action = at.get(
            "/api",
            content_type=None,
            open_when_hidden=None,
            payload=None,
            retry=None,
            retry_interval_ms=None,
            retry_scaler=None,
            retry_max_wait_ms=None,
            retry_max_count=None,
            request_cancellation=None,
        )

        assert action == "@get('/api')"

    def test_delete(self):
        action = at.delete("/api/item/123")
        assert action == "@delete('/api/item/123')"

    def test_query(self):
        action = at.query("/search", {"page": "2"}, include="q")
        assert action == ("@query('/search?page=2', {filterSignals: {'include':'q'}})")

    def test_intl_basic(self):
        action = at.intl("number", "$price", {"style": "currency", "currency": "USD"})
        assert (
            action == "@intl('number', $price, {'style':'currency','currency':'USD'})"
        )

    def test_set_all_with_filters(self):
        action = at.set_all("null", include=["draft"], exclude="tmp.*")
        assert action == "@setAll(null, {'include':'draft','exclude':'tmp.*'})"

    def test_actions_take_route_href(self):
        subscribe = Route("GET /rooms/{room_id}/subscribe")
        send = Route("POST /rooms/{room_id}/send")

        assert at.get(subscribe.href("7"), retry="always") == (
            "@get('/rooms/7/subscribe', {retry: 'always'})"
        )
        assert (
            at.post(send.href("7", query={"src": "btn"}, fragment="latest"))
            == "@post('/rooms/7/send?src=btn#latest')"
        )


class TestDatastarScriptTag:
    """Test the shared Datastar CDN script helper."""

    def test_ModuleScript_uses_cdn(self):
        html = render(ModuleScript())

        assert 'type="module"' in html
        assert f'src="{DATASTAR_CDN_URL}"' in html

    def test_ModuleScript_accepts_custom_src(self):
        html = render(ModuleScript("/static/vendor/datastar.js"))

        assert 'src="/static/vendor/datastar.js"' in html


class TestBackslashNeverPrecedesAClosingQuote:
    """Datastar's statement splitter takes `\\"` for an escaped quote without
    counting backslashes; a value ending in one must not produce it."""

    def test_string_literal(self) -> None:
        from stario.datastar.format import string_literal

        assert string_literal("evil\\") == "'evil\\x5c'"
        assert string_literal("a\\'b") == "'a\\x5c\\'b'"

    def test_signals_attribute(self) -> None:
        import html
        import json

        payload = {"name": "evil\\", "bio": 'hi; "there"\\\\', "p": "a\\nb"}
        rendered = str(data.signals(payload).rendered)
        raw = html.unescape(rendered[len(" data-signals='") : -1])
        assert '\\\\"' not in raw
        assert json.loads(raw) == payload
