"""Escaping fixes from the 2026-11-16 audit (notes/escaping-rules.md)."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from stario import App, Context, Route, SafeString, Writer, baked, h, render
from stario.datastar import SSE, data, literal
from stario.exceptions import StarioError
from stario.json import dumps, dumps_script
from stario.markup import aria, styles
from stario.testing import TestClient
from tests.helpers import TestWriter


class TestSafeStringAttributeValue:
    def test_baked_output_cannot_end_the_attribute(self) -> None:
        @baked
        def tip(text: str):
            return h.Span({"class": "tip"}, text)

        out = render(h.Div({"title": tip('" autofocus onfocus="alert(1)')}))
        assert "onfocus=" not in out.replace("onfocus=&quot;", "")
        assert out.count('"') == 2  # only the quotes around the value

    def test_author_entities_keep_their_meaning(self) -> None:
        assert render(h.Div({"title": SafeString("a&nbsp;b")})) == (
            '<div title="a&nbsp;b"></div>'
        )


class TestUrlSchemes:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            " javascript:alert(1)",
            "\x01javascript:alert(1)",
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "vbscript:msgbox(1)",
            "data:text/html,<script>alert(1)</script>",
        ],
    )
    @pytest.mark.parametrize("key", ["href", "action", "formaction", "HREF"])
    def test_script_urls_raise(self, key: str, url: str) -> None:
        with pytest.raises(StarioError, match="is a script URL"):
            h.A({key: url}, "x")

    def test_src_allows_data_but_not_javascript(self) -> None:
        img = render(h.Img({"src": "data:image/png;base64,AAAA", "alt": ""}))
        assert 'src="data:image/png;base64,AAAA"' in img
        with pytest.raises(StarioError, match="script URL"):
            h.Img({"src": "javascript:alert(1)"})

    @pytest.mark.parametrize(
        "url",
        ["/a?b=c:d", "https://example.com", "mailto:a@b.c", "tel:+48", "#top", "page2"],
    )
    def test_normal_urls_pass(self, url: str) -> None:
        assert render(h.A({"href": url}, "x")).startswith("<a href=")

    def test_safestring_opts_out(self) -> None:
        out = render(h.A({"href": SafeString("javascript:void(0)")}, "x"))
        assert 'href="javascript:void(0)"' in out

    def test_every_attribute_path_checks(self) -> None:
        bad = {"href": "javascript:alert(1)"}
        with pytest.raises(StarioError):  # one-pair path
            h.A(bad)
        with pytest.raises(StarioError):  # several pairs
            h.A({"class": "x", "id": "y", **bad})
        with pytest.raises(StarioError):  # attributes after a child
            h.A("x", bad)

        @baked
        def link(url: str):
            return h.A({"href": url}, "x")

        with pytest.raises(StarioError):  # @baked whole-value slot
            link("javascript:alert(1)")
        assert "/ok" in render(link("/ok"))

    def test_other_attributes_are_not_urls(self) -> None:
        assert 'title="javascript:x"' in render(h.A({"title": "javascript:x"}))


class TestAriaBooleans:
    def test_true_and_false_are_strings(self) -> None:
        out = render(h.Button(aria({"expanded": True, "pressed": False, "x": None})))
        assert out == '<button aria-expanded="true" aria-pressed="false"></button>'


class TestStylesCheckTheRawValue:
    @pytest.mark.parametrize(
        ("value", "wire"),
        [
            ('"Inter", sans-serif', "&quot;Inter&quot;, sans-serif"),
            ('"a b" "c d"', "&quot;a b&quot; &quot;c d&quot;"),
            ("'a & b'", "'a &amp; b'"),
        ],
    )
    def test_quotes_and_ampersands_are_valid_css(self, value: str, wire: str) -> None:
        assert (
            styles({"font-family": value}).rendered == f' style="font-family:{wire};"'
        )

    @pytest.mark.parametrize("value", ["red; color: blue", "x}", "x{", "x\\"])
    def test_breakouts_still_raise(self, value: str) -> None:
        with pytest.raises(StarioError, match="Invalid CSS value"):
            styles({"color": value})


class TestLeadingNewline:
    @pytest.mark.parametrize("tag", [h.Textarea, h.Pre])
    def test_a_leading_newline_survives_the_parser(self, tag: Any) -> None:
        tag_name = render(tag()).split(">")[0][1:]
        assert render(tag("\nfirst")) == f"<{tag_name}>\n\nfirst</{tag_name}>"
        assert render(tag({"id": "t"}, "\nfirst")) == (
            f'<{tag_name} id="t">\n\nfirst</{tag_name}>'
        )

    def test_other_text_is_unchanged(self) -> None:
        assert render(h.Textarea("first\n")) == "<textarea>first\n</textarea>"
        assert render(h.Div("\nx")) == "<div>\nx</div>"


class TestJsonInScript:
    def test_dumps_script_cannot_end_the_script(self) -> None:
        state = {"x": "</script><!--", "y": "a & b", "z": "\u2028"}
        text = dumps_script(state)
        for char in "<>&":
            assert char not in text
        assert json.loads(text) == state
        out = render(h.Script({"type": "application/json"}, text))
        assert out.count("</script>") == 1

    def test_plain_dumps_still_fails_closed(self) -> None:
        with pytest.raises(StarioError, match="dumps_script"):
            h.Script(dumps({"x": "</script>"}))


class TestLiteralNul:
    def test_nul_is_escaped(self) -> None:
        assert literal("a\x00b") == "'a\\x00b'"


class TestBakedRawText:
    def test_safestring_slot_is_verbatim_like_a_direct_call(self) -> None:
        @baked
        def script(code: Any):
            return h.Script(code)

        assert render(script(SafeString("a</script>"))) == render(
            h.Script(SafeString("a</script>"))
        )

    def test_element_slot_raises_like_a_direct_call(self) -> None:
        @baked
        def script(code: Any):
            return h.Script(code)

        with pytest.raises(StarioError, match="raw text, not elements"):
            script(h.B("x"))
        with pytest.raises(StarioError, match="raw text, not elements"):
            h.Script(cast(Any, h.B("x")))

    def test_text_slot_is_still_checked(self) -> None:
        @baked
        def script(code: str):
            return h.Script("var a = '</scr", code, "';")

        with pytest.raises(StarioError, match="would end the element"):
            script("ipt>")


class TestDatastarKeyModifiers:
    @pytest.mark.parametrize(
        "make",
        [
            lambda: data.class_("hidden__case.camel", "$x"),
            lambda: data.attr("title__x", "$x"),
            lambda: data.style("width__x", "$x"),
        ],
    )
    def test_double_underscore_raises(self, make: Any) -> None:
        with pytest.raises(StarioError, match="has '__'"):
            make()


class TestTrustedHtmlStrings:
    def _writer(self) -> TestWriter:
        from tests.helpers import make_writer_raw

        w, _sink, loop = make_writer_raw()
        loop.close()
        return cast(TestWriter, w)

    def test_patch_elements_str_warns_unless_trusted(self) -> None:
        from tests.test_datastar import _make_writer, _sse_body

        w, _sink, loop = _make_writer()
        try:
            with pytest.warns(DeprecationWarning, match="trusted=True"):
                SSE(w).patch_elements("<p id='a'>x</p>")
            SSE(w).patch_elements("<p id='b'>y</p>", trusted=True)
            SSE(w).patch_elements(SafeString("<p id='c'>z</p>"))
            body = _sse_body(w)
            assert body.count(b"datastar-patch-elements") == 3
        finally:
            loop.close()

    async def test_w_html_str_warns_unless_trusted(self) -> None:
        async def warn(_c: Context, w: Writer) -> None:
            w.html("<p>a</p>")

        async def trusted(_c: Context, w: Writer) -> None:
            w.html("<p>b</p>", trusted=True)

        async def safe(_c: Context, w: Writer) -> None:
            w.html(SafeString("<p>c</p>"))

        app = App()
        app.add(Route("GET /warn"), warn)
        app.add(Route("GET /trusted"), trusted)
        app.add(Route("GET /safe"), safe)
        async with TestClient(app) as client:
            with pytest.warns(DeprecationWarning, match="trusted=True"):
                assert (await client.get("/warn")).text == "<p>a</p>"
            assert (await client.get("/trusted")).text == "<p>b</p>"
            assert (await client.get("/safe")).text == "<p>c</p>"
