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
        ],
    )
    @pytest.mark.parametrize(
        "key", ["href", "src", "action", "formaction", "xlink:href", "HREF"]
    )
    def test_javascript_urls_raise(self, key: str, url: str) -> None:
        with pytest.raises(StarioError, match="is a javascript: URL"):
            h.A({key: url}, "x")

    def test_data_urls_pass(self) -> None:
        # Browsers block data: pages at the top level; a data: download
        # link and an inline image are real uses.
        img = render(h.Img({"src": "data:image/png;base64,AAAA", "alt": ""}))
        assert 'src="data:image/png;base64,AAAA"' in img
        link = render(h.A({"href": "data:text/csv,a,b", "download": "x.csv"}, "CSV"))
        assert link == '<a href="data:text/csv,a,b" download="x.csv">CSV</a>'

    @pytest.mark.parametrize(
        "url",
        [
            "/a?b=c:d",
            "https://example.com",
            "mailto:a@b.c",
            "tel:+48",
            "#top",
            "page2",
            "vbscript:msgbox(1)",
            "javascriptx:1",
        ],
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


class TestBooleansArePresence:
    def test_aria_follows_the_one_rule(self) -> None:
        # True writes the bare name, False and None leave it out. An
        # attribute that takes words gets the word as a string.
        out = render(h.Button(aria({"expanded": True, "pressed": False, "x": None})))
        assert out == "<button aria-expanded></button>"
        out = render(h.Button(aria({"expanded": "false"})))
        assert out == '<button aria-expanded="false"></button>'


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

    @pytest.mark.parametrize(
        ("value", "problem"),
        [
            ("url(https://x/a", "unclosed '\\('"),
            ('"Inter', "unclosed quote"),
            ("'Inter", "unclosed quote"),
            ("calc(1px + 2px))", "'\\)' with no"),
            ("[full-start 1fr", "unclosed '\\['"),
            ("1fr]", "'\\]' with no"),
            ("red /*", "comment"),
        ],
    )
    def test_a_value_cannot_swallow_the_next_declaration(
        self, value: str, problem: str
    ) -> None:
        # Unclosed, the browser would read ";display:none;" as part of
        # this value (inside the url, the string or the comment).
        with pytest.raises(StarioError, match=problem):
            styles({"background": value, "display": "none"})

    @pytest.mark.parametrize(
        "value",
        [
            'url("/a.png")',
            "url(/a.png)",
            "calc(100% - var(--gap, 1rem))",
            "[full-start] minmax(0, 1fr) [full-end]",
            '"a)b" serif',
            "'/* not a comment */'",
            "rgb(0 0 0 / 50%)",
        ],
    )
    def test_closed_values_pass(self, value: str) -> None:
        assert styles({"background": value}).rendered.startswith(' style="background:')


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


class TestFinishedHtmlStrings:
    def test_patch_elements_str_is_html_and_warns(self) -> None:
        # 5.8 sent a str as HTML; it still does until 6.0, and warns.
        from tests.test_datastar import _make_writer, _sse_body

        w, _sink, loop = _make_writer()
        try:
            with pytest.warns(DeprecationWarning, match="SafeString"):
                SSE(w).patch_elements("<p id='a'>x</p>")
            SSE(w).patch_elements(SafeString("<p id='c'>z</p>"))
            body = _sse_body(w)
            assert body.count(b"datastar-patch-elements") == 2
            assert b"<p id='a'>x</p>" in body
        finally:
            loop.close()

    async def test_w_html_str_is_html_and_warns(self) -> None:
        async def plain(_c: Context, w: Writer) -> None:
            w.html("<p>a</p>")

        async def safe(_c: Context, w: Writer) -> None:
            w.html(SafeString("<p>c</p>"))

        app = App()
        app.add(Route("GET /plain"), plain)
        app.add(Route("GET /safe"), safe)
        async with TestClient(app) as client:
            with pytest.warns(DeprecationWarning, match=r"w\.html\(str\)"):
                plain_text = (await client.get("/plain")).text
            assert plain_text == "<p>a</p>"
            assert (await client.get("/safe")).text == "<p>c</p>"


class TestAuditFixes:
    """Cases from the audit of the unreleased changes (2026-11-16)."""

    def test_an_empty_safestring_slot_does_not_split_a_baked_script(self) -> None:
        # An empty SafeString writes nothing, so the text on both sides is
        # one run and is checked as one, as in a direct h.Script call.
        @baked
        def page(a: str, b: SafeString, c: str):
            return h.Script(a, b, c)

        args = ("</scr", SafeString(""), "ipt><img src=x onerror=alert(1)>")
        with pytest.raises(StarioError, match="</script"):
            render(h.Script(*args))
        with pytest.raises(StarioError, match="</script"):
            render(page(*args))
        assert render(page("a", SafeString(""), "b")) == "<script>ab</script>"

    @pytest.mark.parametrize("brk", ["\n", "\r", "\f"])
    def test_a_line_break_inside_a_css_quote_raises(self, brk: str) -> None:
        # CSS ends a string at a line break, so the next declarations would
        # become part of this one.
        with pytest.raises(StarioError, match="line break inside a quote"):
            styles({"font-family": f"'a{brk}'", "display": "none"})

    def test_a_line_break_outside_a_quote_is_whitespace(self) -> None:
        assert styles({"margin": "0\n1px"}).rendered == ' style="margin:0\n1px;"'

    @pytest.mark.parametrize(
        ("value", "wire"),
        [
            ("a&nbsp;b", "a&nbsp;b"),
            ('a "b" <c>', "a &quot;b&quot; &lt;c&gt;"),
            ('café "x" & <y>', "café &quot;x&quot; & &lt;y&gt;"),
            ('日本 "x"', "日本 &quot;x&quot;"),
            ('🎉 "x" <y>', "🎉 &quot;x&quot; &lt;y&gt;"),
            ("clean", "clean"),
        ],
    )
    def test_safestring_attribute_values_keep_ampersands(
        self, value: str, wire: str
    ) -> None:
        assert (
            render(h.Div({"title": SafeString(value)})) == f'<div title="{wire}"></div>'
        )

    @pytest.mark.parametrize(
        "key",
        ["alt", "aria-label", "hidden", "height", "accept", "size", "step", "hrefx"],
    )
    def test_other_keys_with_a_colon_pass(self, key: str) -> None:
        assert "javascript:x" in render(h.Div({key: "javascript:x"}))

    @pytest.mark.parametrize(
        "key", ["Href", "SRC", "Action", "FormAction", "XLINK:HREF"]
    )
    def test_url_keys_match_in_any_case(self, key: str) -> None:
        with pytest.raises(StarioError, match="javascript: URL"):
            h.A({key: "javascript:alert(1)"})

    def test_raw_text_without_a_tag_start_passes(self) -> None:
        assert render(h.Script("if (a < b) go()")) == "<script>if (a < b) go()</script>"
        with pytest.raises(StarioError):
            h.Script("x = '</SCRIPT>'")
        with pytest.raises(StarioError):
            h.Script("x = '<!--'")

    def test_classes_as_a_value_still_checks_the_name(self) -> None:
        from stario.markup import classes

        assert render(h.Div({"class": classes("a", "b")})) == '<div class="a b"></div>'
        with pytest.raises(StarioError, match="other attributes"):
            h.Div({"id": classes("a")})

    def test_literal_writes_enum_members_as_their_values(self) -> None:
        import enum

        class Color(enum.StrEnum):
            RED = "red"

        class N(enum.IntEnum):
            ONE = 1

        assert literal(Color.RED) == "'red'"
        assert literal({"k": Color.RED, Color.RED: N.ONE}) == "{'k':'red','red':1}"
        assert literal(N.ONE) == "1"
