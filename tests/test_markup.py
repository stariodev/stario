"""Tests for stario.markup — HTML generation and rendering."""

import dataclasses
from typing import Any, cast

import pytest

from stario.exceptions import StarioError
from stario.markup import (
    Attrs,
    Comment,
    SafeString,
    Tag,
    aria,
    baked,
    classes,
    data,
    render,
    styles,
)
from stario.markup import html as h
from stario.markup.attributes import prefixed
from stario.markup.escape import (
    escape_attribute_value,
    escape_sq_attribute_value,
    escape_text,
    validate_attribute_key,
)
from stario.markup.slots import BakeSlot

A = h.A
Body = h.Body
Br = h.Br
Button = h.Button
Col = h.Col
Div = h.Div
Head = h.Head
Html = h.Html
HtmlDocument = h.HtmlDocument
Img = h.Img
Input = h.Input
Li = h.Li
Meta = h.Meta
P = h.P
Script = h.Script
Span = h.Span
Title = h.Title
Ul = h.Ul


class TestEscapeAttributeValue:
    """Test attribute-value escaping helper."""

    def test_single_quote_stays_literal(self):
        # Values are always emitted inside double quotes, so ' cannot break out.
        assert escape_attribute_value("say 'hello'") == "say 'hello'"

    def test_escape_all_characters(self):
        result = escape_attribute_value("<script>alert('\"XSS\" & bad')</script>")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result
        assert "&quot;" in result
        assert "'" in result  # literal, see above


class TestEscapeSqAttributeValue:
    """Test single-quoted attribute-value escaping helper."""

    def test_escape_apostrophe(self):
        assert escape_sq_attribute_value("it's") == "it&#39;s"

    def test_escape_combined_payload(self):
        result = escape_sq_attribute_value('{"x":"y"} & <z>')
        assert "&#39;" not in result  # no apostrophe in input
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result


class TestEscapeText:
    """Test text-node escaping helper."""

    def test_text_escapes_markup_chars(self):
        assert escape_text("a < b & c > d") == "a &lt; b &amp; c &gt; d"


class TestValidateAttributeKey:
    """Attribute names are validated, never mangled (entities are not decoded in names)."""

    def test_framework_shorthand_keys_allowed(self):
        # Alpine/Vue-style names and Datastar modifier suffixes are legal.
        assert validate_attribute_key("@click") == "@click"
        assert validate_attribute_key(":class") == ":class"
        assert (
            validate_attribute_key("data-on-click__debounce.500ms")
            == "data-on-click__debounce.500ms"
        )

    @pytest.mark.parametrize(
        "bad_key",
        [
            "onclick=alert()",
            "class name",
            r"a\b`c",
            'x"y',
            "x'y",
            "a<b",
            "a>b",
            "a/b",
            "a&b",
            "a\nb",
            "a\x00b",
            "",
        ],
    )
    def test_invalid_keys_raise(self, bad_key):
        with pytest.raises(StarioError, match="Invalid attribute name"):
            validate_attribute_key(bad_key)


class TestSvgNamespace:
    """SVG tags live on `stario.markup.svg`."""

    def test_svg_submodule(self):
        from stario.markup import svg

        assert render(svg.Svg(svg.Circle())) == "<svg><circle/></svg>"

    def test_svg_leaf_self_closes_without_children(self):
        from stario.markup import svg

        assert render(svg.Circle()) == "<circle/>"
        assert render(svg.Circle({"cx": "1", "cy": "2", "r": "3"})) == (
            '<circle cx="1" cy="2" r="3"/>'
        )
        assert render(svg.Path()) == "<path/>"
        assert render(svg.FeGaussianBlur({"stdDeviation": "2"})) == (
            '<feGaussianBlur stdDeviation="2"/>'
        )

    def test_svg_leaf_with_children_uses_open_close(self):
        from stario.markup import svg

        assert render(svg.Circle(svg.Title("dot"))) == (
            "<circle><title>dot</title></circle>"
        )
        assert render(svg.Path(svg.Title("t"))) == "<path><title>t</title></path>"
        assert render(
            svg.FeGaussianBlur(
                {"stdDeviation": "2"},
                svg.FeMergeNode(),
            )
        ) == ('<feGaussianBlur stdDeviation="2"><feMergeNode/></feGaussianBlur>')

    def test_svg_void_vs_self_closing_when_empty(self):
        # void rejects children; self_closing_when_empty allows them.
        from stario.markup import svg

        custom_void = Tag("circle", empty="void")
        custom_leaf = Tag("circle", empty="self_closing_when_empty")

        assert render(custom_void()) == "<circle/>"
        assert render(custom_leaf()) == "<circle/>"

        with pytest.raises(StarioError, match="void element and cannot have children"):
            custom_void(svg.Title("nope"))

        assert (
            render(custom_leaf(svg.Title("ok"))) == "<circle><title>ok</title></circle>"
        )

    def test_svg_script_and_mesh_tags(self):
        from stario.markup import svg

        assert render(svg.Script()) == "<script></script>"
        assert render(svg.Script("x=1")) == "<script>x=1</script>"
        assert render(svg.Mesh()) == "<mesh/>"


class TestSafeString:
    """Test SafeString for unescaped content."""

    def test_safestring_is_frozen(self):
        s = SafeString("x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.rendered = "y"  # type: ignore[misc]

    def test_safestring_not_escaped(self):
        safe = SafeString("<b>bold</b>")
        result = render(h.Div(safe))
        assert result == "<div><b>bold</b></div>"

    def test_regular_string_escaped(self):
        result = render(h.Div("<b>bold</b>"))
        assert "&lt;b&gt;" in result


class TestAttrs:
    def test_attrs_is_frozen(self):
        attrs = Attrs(' class="x"')
        with pytest.raises(dataclasses.FrozenInstanceError):
            attrs.rendered = ""  # type: ignore[misc]

    def test_attrs_fragment_is_inserted_in_opening_tag(self):
        result = render(h.Div({"id": "main"}, Attrs(' data-x="1"'), "content"))
        assert result == '<div id="main" data-x="1">content</div>'

    def test_attrs_must_appear_before_children(self):
        with pytest.raises(
            StarioError, match="attributes must be passed before children"
        ):
            h.Div("content", Attrs(' class="late"'))


class TestComment:
    """Test HTML comment node helper."""

    def test_comment_renders_in_tree(self):
        result = render(h.Div(Comment("server-note"), h.P("Hello")))
        assert result == "<div><!--server-note--><p>Hello</p></div>"

    def test_comment_escapes_dangerous_sequences(self):
        result = render(Comment("--><script>alert(1)</script>"))
        assert result == "<!----&gt;&lt;script&gt;alert(1)&lt;/script&gt;-->"

    def test_comment_rejects_boolean_content(self):
        with pytest.raises(StarioError, match="Invalid comment content type: bool"):
            Comment(True)

    def test_comment_rejects_non_exact_numeric_content(self):
        from decimal import Decimal

        with pytest.raises(StarioError, match="Invalid comment content type: Decimal"):
            Comment(cast(Any, Decimal("1.5")))


class TestTagCreation:
    """Test Tag class and element creation."""

    def test_tag_name_is_validated(self):
        with pytest.raises(StarioError, match="Invalid tag name"):
            Tag("div onclick=alert(1)")

        with pytest.raises(TypeError):
            Tag("html", prelude="<!doctype html>")  # type: ignore[call-arg]

    def test_raw_tag_requires_call(self):
        with pytest.raises(StarioError, match="Tag object directly"):
            render(cast(Any, Tag("div")))

    def test_consecutive_attribute_mappings_are_not_merged(self):
        result = render(h.Div({"class": "first"}, {"class": "second"}))
        assert result == '<div class="first" class="second"></div>'

    def test_tag_multiple_children(self):
        result = render(h.Div(h.P("one"), h.P("two")))
        assert result == "<div><p>one</p><p>two</p></div>"


class TestAttributeTypes:
    """Test different attribute value types."""

    def test_integer_attribute(self):
        result = render(h.Input({"tabindex": 0}))
        assert 'tabindex="0"' in result

    def test_true_boolean_attribute(self):
        result = render(h.Input({"disabled": True}))
        assert "disabled" in result
        assert "disabled=" not in result  # No value for true booleans

    def test_false_boolean_attribute(self):
        result = render(h.Input({"disabled": False}))
        assert "disabled" not in result

    def test_none_attribute_is_omitted(self):
        # None means "no value here" — same as False and same as None in
        # token lists / child position. True is the only bare-attribute spelling.
        result = render(h.Input({"required": None}))
        assert "required" not in result

    def test_list_attribute_is_rejected_by_core(self):
        with pytest.raises(StarioError, match="scalar values only"):
            render(h.Div(cast(Any, {"class": ["btn", "primary"]})))

    def test_dict_attribute_is_rejected_by_core(self):
        with pytest.raises(StarioError, match="scalar values only"):
            render(h.Div(cast(Any, {"data": {"flag": True}})))

    def test_classes_helper(self):
        attrs = classes("btn", "primary", "large")
        assert attrs == Attrs(' class="btn primary large"')
        result = render(h.Div(attrs))
        assert 'class="btn primary large"' in result

    def test_classes_helper_skips_none_and_false(self):
        result = render(h.Div(classes("btn", None, False, "primary")))
        assert 'class="btn primary"' in result

    def test_classes_helper_rejects_true_item(self):
        with pytest.raises(StarioError, match="Invalid class token type: bool"):
            render(h.Div(classes("btn", cast(Any, True))))

    def test_classes_helper_all_skipped_yields_empty_value(self):
        result = render(h.Div(classes(None, False)))
        assert 'class=""' in result

    def test_classes_helper_conditional_dict(self):
        result = render(
            h.Div(classes({"btn": True, "active": False, "primary": 1, "ghost": None}))
        )
        assert 'class="btn primary"' in result

    def test_classes_helper_rejects_non_string_mapping_key(self):
        with pytest.raises(StarioError, match="Invalid class token type: int"):
            render(h.Div(classes(cast(Any, {1: True}))))

    def test_classes_helper_tokens_are_escaped(self):
        result = render(h.Div(classes({'a"b': True})))
        assert 'class="a&quot;b"' in result

    def test_style_dict_attribute_is_rejected_by_core(self):
        with pytest.raises(StarioError, match="scalar values only"):
            render(h.Div(cast(Any, {"style": {"color": "red"}})))

    def test_styles_helper(self):
        attrs = styles({"color": "red", "font-size": "16px"})
        assert type(attrs) is Attrs
        result = render(h.Div(attrs))
        assert 'style="' in result
        assert "color:red;" in result
        assert "font-size:16px;" in result

    def test_styles_helper_rejects_invalid_values(self):
        with pytest.raises(StarioError, match="Invalid CSS value type"):
            render(h.Div(styles(cast(Any, {"color": ["red", "blue"]}))))

    def test_styles_helper_rejects_css_separators_in_keys(self):
        with pytest.raises(StarioError, match="Invalid CSS property name"):
            render(h.Div(styles({"x;background:red": "1"})))

    def test_styles_helper_rejects_css_separators_in_values(self):
        with pytest.raises(StarioError, match="Invalid CSS value"):
            render(h.Div(styles({"color": "red;position:fixed"})))

    def test_styles_helper_rejects_backslash_in_values(self):
        with pytest.raises(StarioError, match="Invalid CSS value"):
            render(h.Div(styles({"color": r"red\3b color:blue"})))

    def test_styles_helper_rejects_non_string_keys(self):
        with pytest.raises(StarioError, match="Invalid CSS property name type"):
            render(h.Div(styles(cast(Any, {1: "red"}))))

    def test_styles_helper_rejects_at_rules(self):
        with pytest.raises(StarioError, match="do not support at-rules"):
            render(h.Div(styles({"@media": "screen"})))

    def test_data_helper(self):
        attrs = data({"user-id": "123", "role": "admin"})
        assert type(attrs) is Attrs
        result = render(h.Div(attrs))
        assert 'data-user-id="123"' in result
        assert 'data-role="admin"' in result

    def test_data_helper_key_is_validated(self):
        with pytest.raises(StarioError, match="Invalid attribute name"):
            render(h.Div(data({"bad key=1": "x"})))

    def test_aria_helper(self):
        attrs = aria({"label": "Close"})
        assert type(attrs) is Attrs
        result = render(h.Button(attrs, "×"))
        assert 'aria-label="Close"' in result

    def test_prefixed_helper(self):
        attrs = prefixed("aria", {"expanded": True, "hidden": False})
        result = render(h.Div(attrs))
        assert "aria-expanded" in result
        assert "aria-hidden" not in result

    def test_attribute_key_rejects_safestring(self):
        with pytest.raises(StarioError, match="Invalid attribute name type"):
            render(h.Div(cast(Any, {SafeString("x<y"): "x"})))


class TestRender:
    """Test the render function."""

    def test_render_multiple_elements(self):
        result = render(h.P("one"), h.P("two"), h.P("three"))
        assert result == "<p>one</p><p>two</p><p>three</p>"

    def test_render_text_escaping(self):
        result = render(h.P("<script>alert('xss')</script>"))
        assert "&lt;script&gt;" in result
        assert "&lt;/script&gt;" in result

    def test_render_integer(self):
        result = render(h.Span(42))
        assert result == "<span>42</span>"

    def test_render_rejects_boolean_child(self):
        with pytest.raises(
            StarioError, match="Boolean values are not valid HTML child content"
        ):
            render(h.Span(True))

    def test_render_rejects_uncalled_tag_child(self):
        with pytest.raises(StarioError, match="Cannot render a Tag object directly"):
            render(h.Div(cast(Any, h.Div)))

    def test_render_rejects_plain_tuple(self):
        with pytest.raises(StarioError, match="Cannot render a tuple as HTML content"):
            render(cast(Any, ("<div>", [])))

    def test_four_tuple_child_is_not_an_element(self):
        # A database row with four columns is a tuple, not markup. Before the
        # Element type, this rendered the row's text as raw tag/attribute bytes.
        row = ("<img src=x onerror=alert(1)>", " onclick=evil", "abc", "")
        with pytest.raises(StarioError, match="Cannot render a tuple as HTML content"):
            render(h.Li(cast(Any, row)))
        assert render(h.Li(*row)) == (
            "<li>&lt;img src=x onerror=alert(1)&gt; onclick=evilabc</li>"
        )

    def test_elements_are_tuples_built_only_by_tags(self):
        from stario.markup.types import Element

        element = h.P({"id": "x"}, "hi")
        assert type(element) is Element
        assert isinstance(element, tuple)
        assert len(element) == 4
        assert render(element) == '<p id="x">hi</p>'


class TestHtmlTags:
    """Test predefined HTML tags."""

    def test_html_document_has_doctype(self):
        result = render(h.HtmlDocument(h.Head(h.Title("Test")), h.Body(h.P("Hello"))))
        assert result.startswith("<!doctype html>")
        assert "<html>" in result

    def test_void_elements_reject_children(self):
        with pytest.raises(StarioError, match="void element and cannot have children"):
            h.Br("text")
        with pytest.raises(StarioError, match="void element and cannot have children"):
            h.Img({"src": "a.png"}, "caption")


class TestBakedDecorator:
    """@baked builders return rendered SafeString fragments."""

    def test_positional_and_render(self):
        @baked
        def layout(title, body):
            return h.Div(h.Title(title), body)

        result = layout("Hi", h.P("w"))
        assert result == SafeString("<div><title>Hi</title><p>w</p></div>")
        assert render(result) == "<div><title>Hi</title><p>w</p></div>"

    def test_keyword_arguments(self):
        @baked
        def layout(title, body):
            return h.Div(h.Title(title), body)

        assert (
            render(layout(body=h.P("x"), title="T"))
            == "<div><title>T</title><p>x</p></div>"
        )

    def test_keyword_only_parameters(self):
        @baked
        def layout(*, title, body):
            return h.Div(h.Title(title), body)

        assert (
            render(layout(title="A", body=h.P("b")))
            == "<div><title>A</title><p>b</p></div>"
        )

    def test_default_none_omits_child(self):
        @baked
        def block(inner=None):
            return h.Div(h.P("pre"), inner)

        assert render(block()) == "<div><p>pre</p></div>"
        assert render(block(h.P("mid"))) == "<div><p>pre</p><p>mid</p></div>"

    def test_static_only_no_slots(self):
        @baked
        def shell():
            return h.Div(h.P("fixed"))

        assert shell() == SafeString("<div><p>fixed</p></div>")
        assert render(shell()) == "<div><p>fixed</p></div>"

    def test_rejects_static_boolean_child(self):
        def bad():
            return h.Div(True)

        with pytest.raises(
            StarioError,
            match=r"@baked .+\.bad: boolean literal is not valid HTML child content",
        ):
            baked(bad)

    def test_rejects_static_uncalled_tag_child(self):
        def bad():
            return h.Div(cast(Any, h.Div))

        with pytest.raises(
            StarioError,
            match=r"@baked .+\.bad: uncalled Tag factory in child position",
        ):
            baked(bad)

    def test_rejects_unused_parameter(self):
        def bad(label, unused):
            return h.Li(label)

        with pytest.raises(
            StarioError,
            match=r"@baked .+\.bad: parameter 'unused' is never used in the template",
        ):
            baked(bad)

    def test_rejects_var_positional(self):

        def bad(*a):
            return h.Div()

        with pytest.raises(StarioError, match="\\*args"):
            baked(bad)

    def test_rejects_var_keyword(self):

        def bad(**kw):
            return h.Div()

        with pytest.raises(StarioError, match="\\*\\*kwargs"):
            baked(bad)

    def test_rejects_positional_only(self):

        def bad(x, /):
            return h.Div(str(x))

        with pytest.raises(StarioError, match="positional-only"):
            baked(bad)

    def test_bakeslot_left_in_tree_raises(self):
        with pytest.raises(StarioError, match="Unfilled @baked slot"):
            render(h.Div(cast(Any, BakeSlot("t"))))

    def test_slotted_tag_outside_baked_raises(self):
        # A Tag called with a slot value produces an internal slotted node that
        # only @baked may consume; render must reject it loudly.
        slotted = h.A(cast(Any, {"href": BakeSlot("url")}), "x")
        with pytest.raises(StarioError, match="Unfilled @baked attribute slot"):
            render(cast(Any, slotted))

    def test_defaults_applied_on_positional_call(self):
        @baked
        def block(main, aside=None):
            return h.Div(main, aside)

        assert render(block(h.P("m"))) == "<div><p>m</p></div>"
        assert render(block(h.P("m"), h.P("a"))) == "<div><p>m</p><p>a</p></div>"

    def test_call_errors_are_native_python_semantics(self):
        # The splice is generated with the builder's exact signature, so the
        # interpreter itself produces standard TypeErrors naming the builder.
        @baked
        def layout(title, body):
            return h.Div(h.Title(title), body)

        bad_call = cast(Any, layout)
        with pytest.raises(
            TypeError,
            match=r"layout\(\) missing 1 required positional argument: 'body'",
        ):
            bad_call("only-title")
        with pytest.raises(
            TypeError, match=r"takes 2 positional arguments but 3 were given"
        ):
            bad_call("a", "b", "c")
        with pytest.raises(
            TypeError, match=r"got an unexpected keyword argument 'nope'"
        ):
            bad_call("a", body="b", nope=1)
        with pytest.raises(
            TypeError, match=r"got multiple values for argument 'title'"
        ):
            bad_call("a", "b", title="z")

    def test_wrapper_preserves_identity_and_signature(self):
        import inspect

        @baked
        def layout(title, body=None):
            return h.Div(h.Title(title), body)

        assert layout.__name__ == "layout"
        assert layout.__wrapped__.__name__ == "layout"  # type: ignore[attr-defined]
        assert list(inspect.signature(layout).parameters) == ["title", "body"]

    def test_reserved_parameter_prefix_rejected(self):
        # Builder defined at module level: class bodies name-mangle
        # double-underscore identifiers, which would defeat the check here.
        with pytest.raises(StarioError, match="collides with generated internals"):
            baked(_builder_with_reserved_name)

    def test_static_builder_rejects_arguments(self):
        @baked
        def shell():
            return h.Div(h.P("fixed"))

        with pytest.raises(TypeError):
            cast(Any, shell)("unexpected")

    def test_dynamic_child_slot_value_kinds_return_safestring(self):
        def builder(content):
            return h.Section("before ", content, " after")

        compiled = baked(builder)

        values = [
            None,
            "Tom & Jerry",
            SafeString("<strong>trusted</strong>"),
            7,
            2.5,
            h.Em("tag"),
            [h.B("a"), " & ", h.I("b")],
        ]
        for value in values:
            assert compiled(value) == SafeString(render(builder(value))), value

    def test_dynamic_child_slot_rejects_invalid_values_at_call_time(self):
        @baked
        def block(content):
            return h.Div(content)

        with pytest.raises(StarioError, match="Boolean values are not valid"):
            block(True)

        with pytest.raises(StarioError, match="Cannot render a Tag object directly"):
            block(h.Div)

    def test_baked_views_compose_without_double_escaping(self):
        @baked
        def row(label):
            return h.Li(label)

        @baked
        def page(rows):
            return h.Ul(rows)

        result = page([row("A & B"), row(SafeString("<b>C</b>"))])

        assert result == SafeString("<ul><li>A &amp; B</li><li><b>C</b></li></ul>")
        assert render(h.Main(result)) == (
            "<main><ul><li>A &amp; B</li><li><b>C</b></li></ul></main>"
        )


class TestBakedAttributeSlots:
    """Parameters as attribute values: rendered per call via the shared ladder."""

    def test_whole_value_slot_escapes(self):
        @baked
        def link(href):
            return h.A({"href": href}, "go")

        assert (
            render(link('/x?a=1&b="2"'))
            == '<a href="/x?a=1&amp;b=&quot;2&quot;">go</a>'
        )

    def test_whole_value_slot_boolean_presence(self):
        @baked
        def save(disabled):
            return h.Button({"disabled": disabled}, "Save")

        assert render(save(True)) == "<button disabled>Save</button>"
        assert render(save(False)) == "<button>Save</button>"
        assert render(save(None)) == "<button>Save</button>"

    def test_whole_value_slot_rejects_list(self):
        @baked
        def box(classes):
            return h.Div({"class": classes})

        with pytest.raises(StarioError, match="scalar values only"):
            render(box(["a", None, "b"]))

    def test_whole_value_slot_rejects_class_dict(self):
        @baked
        def box(classes):
            return h.Div({"class": classes})

        with pytest.raises(StarioError, match="scalar values only"):
            render(box({"on": True, "off": False}))

    def test_whole_value_slot_accepts_rendered_style_value(self):
        @baked
        def box(style_value):
            return h.Div({"style": style_value})

        assert render(box(SafeString("color:red;"))) == (
            '<div style="color:red;"></div>'
        )

    def test_whole_value_slot_rejects_nested_dict(self):
        @baked
        def box(data):
            return h.Div({"data": data})

        with pytest.raises(StarioError, match="scalar values only"):
            render(box({"id": 5, "on": True}))

    def test_same_parameter_in_attribute_and_child(self):
        @baked
        def labeled(label):
            return h.Div({"aria-label": label}, label)

        assert render(labeled("Hi & bye")) == (
            '<div aria-label="Hi &amp; bye">Hi &amp; bye</div>'
        )

    def test_slot_on_empty_element(self):
        @baked
        def field(name):
            return h.Input({"type": "text", "name": name})

        assert render(field("user")) == '<input type="text" name="user"/>'

    def test_kwargs_call_path(self):
        @baked
        def link(href, label, state):
            return h.A({"href": href, "class": state}, label)

        assert (
            render(link(label="L", href="/k", state="s"))
            == '<a href="/k" class="s">L</a>'
        )

    def test_slot_as_attribute_name_rejected(self):
        def bad(key):
            return h.Div({key: "x"})

        with pytest.raises(
            StarioError,
            match=r"cannot be used as attribute names|cannot use .* as a dict key|unhashable type",
        ):
            baked(bad)

    def test_slot_inside_styles_helper_rejected(self):
        def bad(color):
            return h.Div(styles({"color": color}))

        with pytest.raises(StarioError, match="not supported inside styles"):
            baked(bad)

    def test_slot_inside_data_helper_rejected(self):
        def bad(user_id):
            return h.Div(data({"id": user_id}))

        with pytest.raises(StarioError, match="not supported inside data"):
            baked(bad)

    def test_slot_as_class_dict_condition_rejected(self):
        def bad(on):
            return h.Div(classes({"active": on}))

        with pytest.raises(
            StarioError, match="not supported inside classes\\(\\) conditional"
        ):
            baked(bad)


class TestBakedRawText:
    """A parameter inside h.Script / h.Style splices verbatim, with the breakout check."""

    def test_script_parameter_is_verbatim(self):
        @baked
        def inline(code):
            return h.Script(code)

        assert render(inline("a<b && c")) == "<script>a<b && c</script>"
        assert render(inline("if (a > b) { go(); }")) == (
            "<script>if (a > b) { go(); }</script>"
        )

    def test_script_parameter_cannot_end_the_element(self):
        @baked
        def inline(code):
            return h.Script(code)

        with pytest.raises(StarioError, match="would end the element early"):
            inline("</script>")
        with pytest.raises(StarioError, match="would end the element early"):
            inline('x("</SCRIPT >")')
        with pytest.raises(StarioError, match="would end the element early"):
            inline("<!-- x -->")

    def test_style_parameter_is_verbatim_and_checked(self):
        @baked
        def sheet(css):
            return h.Style(css)

        assert render(sheet("a > b { color: red; }")) == (
            "<style>a > b { color: red; }</style>"
        )
        with pytest.raises(StarioError, match="would end the element early"):
            sheet("</style><script>x()</script>")

    def test_breakout_check_joins_literal_text_and_values(self):
        # Literal text around the slot is part of the same run on the wire.
        @baked
        def inline(code):
            return h.Script("</scr", code, "ipt>")

        assert render(inline("X")) == "<script></scrXipt></script>"
        with pytest.raises(StarioError, match="would end the element early"):
            inline("")

        @baked
        def pair(a, b):
            return h.Script(a, b)

        assert render(pair("a<", "b")) == "<script>a<b</script>"
        with pytest.raises(StarioError, match="would end the element early"):
            pair("</scr", "ipt>")

    def test_literal_breakout_is_rejected_at_decoration(self):
        with pytest.raises(StarioError, match="would end the element early"):

            @baked
            def inline(code):
                return h.Script("</script>", code)

    def test_raw_slot_value_ladder(self):
        @baked
        def inline(code):
            return h.Script("var x = ", code, ";")

        assert render(inline(None)) == "<script>var x = ;</script>"
        assert render(inline(42)) == "<script>var x = 42;</script>"
        assert render(inline(["a", "<", "b"])) == "<script>var x = a<b;</script>"
        assert render(inline(SafeString("'<'"))) == "<script>var x = '<';</script>"

    def test_raw_text_attributes_and_siblings_still_escape(self):
        @baked
        def page(src, code, label):
            return h.Div(h.Script({"src": src}, code), h.P(label))

        assert render(page('/a.js?x="1"&y', "a<b", "<p>")) == (
            '<div><script src="/a.js?x=&quot;1&quot;&amp;y">a<b</script>'
            "<p>&lt;p&gt;</p></div>"
        )

    def test_parameter_in_raw_run_counts_as_used(self):
        # `code` is used inside the raw run; only `unused` is reported.
        with pytest.raises(StarioError, match="'unused' is never used") as info:

            @baked
            def inline(code, unused):
                return h.Script(code)

        assert info.value.context["parameter"] == "unused"

    def test_unfilled_raw_slot_cannot_be_rendered(self):
        def builder(code):
            return h.Script("x = ", code)

        tree = builder(BakeSlot("code"))
        with pytest.raises(StarioError, match="Unfilled @baked slot") as info:
            render(tree)
        assert info.value.context["slot_name"] == "code"


class TestBakedDifferential:
    """baked(f)(args) must render byte-identical to the undecorated f(args)."""

    ADVERSARIAL = (
        "plain",
        '"><script>alert(1)</script>',
        "it's & a<b>c</b>",
        "",
        0,
        -1,
        2.5,
        SafeString("<b>trusted</b>"),
    )

    def test_child_and_attribute_equivalence(self):
        def builder(href, label, state):
            return h.Div(
                {"id": "wrap"},
                h.A({"href": href, "class": state, "data-n": 3}, label),
                h.P(label),
            )

        compiled = baked(builder)

        states = ["on", None, False, 9, SafeString("raw"), 'q"q']
        for value in self.ADVERSARIAL:
            for state in states:
                href = value if isinstance(value, str) else "/x"
                expected = render(builder(href, value, state))
                actual = render(compiled(href, value, state))
                assert actual == expected, (href, value, state)

    def test_attribute_value_kinds_equivalence(self):
        def builder(v):
            return h.Div({"data-v": v})

        compiled = baked(builder)

        for value in [*self.ADVERSARIAL, True, False, None]:
            expected = render(builder(value))
            actual = render(compiled(value))
            assert actual == expected, value


class TestBakeSlotGuards:
    """Misusing a parameter at bake time fails loudly at decoration."""

    def test_fstring_formatting_rejected(self):
        def bad(item_id):
            return h.A({"href": f"/items/{item_id}"}, "view")

        with pytest.raises(StarioError, match="string formatting"):
            baked(bad)

    def test_str_call_rejected(self):
        def bad(n):
            return h.P(str(n))

        with pytest.raises(StarioError, match="f-string formatting"):
            baked(bad)

    def test_concatenation_rejected(self):
        def bad(name):
            return h.P("hello " + name)

        with pytest.raises(StarioError, match="concatenation"):
            baked(bad)

    def test_percent_formatting_rejected(self):
        # str.__mod__ stringifies the operand itself, so this trips the
        # __str__ guard rather than __rmod__ — loud either way.
        def bad(name):
            return h.P(f"hello {name}")

        with pytest.raises(StarioError, match="cannot be used with"):
            baked(bad)

    def test_truthiness_rejected(self):
        def bad(active):
            return h.Div("on" if active else "off")

        with pytest.raises(StarioError, match="truthiness"):
            baked(bad)

    def test_iteration_rejected(self):
        def bad(items):
            return h.Ul(*[h.Li(i) for i in items])

        with pytest.raises(StarioError, match="iteration"):
            baked(bad)

    def test_subscripting_rejected(self):
        def bad(items):
            return h.P(items[0])

        with pytest.raises(StarioError, match="subscripting"):
            baked(bad)

    def test_attribute_access_rejected(self):
        def bad(user):
            return h.P(user.name)

        with pytest.raises(StarioError, match="attribute access"):
            baked(bad)

    def test_equality_rejected(self):
        def bad(href):
            return (
                h.A({"href": href}, "home")
                if href == "/"
                else h.A({"href": href}, "other")
            )

        with pytest.raises(StarioError, match="equality"):
            baked(bad)

    def test_is_not_none_does_not_guard_at_bake_time(self):
        """`is` / `is not` bypass slot guards — branches freeze at decoration."""

        def block(inner=None):
            return h.Div(inner if inner is not None else h.P("fallback"))

        fn = baked(block)
        assert render(fn()) == "<div></div>"
        assert render(fn(h.P("x"))) == "<div><p>x</p></div>"

    def test_len_rejected(self):
        def bad(items):
            return h.P(len(items))

        with pytest.raises(StarioError, match="len\\(\\)"):
            baked(bad)


def _builder_with_reserved_name(__stario_x):
    return h.Div(__stario_x)


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_none_child_ignored(self):
        result = render(h.Div(None, "text", None))
        assert result == "<div>text</div>"

    def test_mixed_attributes_and_children(self):
        with pytest.raises(
            StarioError, match="attributes must be passed before children"
        ):
            render(h.Div({"id": "1"}, "text", {"class": "test"}))

    def test_text_content_keeps_quotes_literal(self):
        result = render(h.P("say \"hello\" and 'bye'"))
        assert result == "<p>say \"hello\" and 'bye'</p>"

    def test_safestring_attribute(self):
        result = render(h.Div({"data-raw": SafeString("raw<>value")}))
        assert 'data-raw="raw<>value"' in result


class TestTrustBoundaries:
    """Pin where escaping stops and trusted content begins."""

    def test_comment_safestring_is_trusted_verbatim(self):
        # SafeString is the explicit trust opt-out: a `-->` inside it CAN
        # close the comment early. Callers own the content.
        assert render(Comment(SafeString("-->raw"))) == "<!---->raw-->"

    def test_comment_str_cannot_break_out(self):
        result = render(Comment("evil --> <script>x</script>"))
        assert result == "<!--evil --&gt; &lt;script&gt;x&lt;/script&gt;-->"

    def test_comment_numeric_content(self):
        assert render(Comment(42)) == "<!--42-->"

    def test_comment_rejects_bool(self):
        with pytest.raises(StarioError, match="bool"):
            Comment(True)

    def test_script_str_children_are_raw_text(self):
        # <script> is a raw-text element: the browser never decodes entities
        # inside it, so escaping would silently break the JavaScript.
        result = render(h.Script("if (a < b && c) { go(); }"))
        assert result == "<script>if (a < b && c) { go(); }</script>"

    def test_script_rejects_text_that_closes_the_element(self):
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script('x("</script>")')
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script('x("</SCRIPT >")')
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script("<!-- x -->")

    def test_script_list_children_are_raw_text(self):
        result = render(h.Script(["a < b;", "c > d;"]))
        assert result == "<script>a < b;c > d;</script>"

    def test_style_str_children_are_raw_text(self):
        result = render(h.Style("a > b { color: red; }"))
        assert result == "<style>a > b { color: red; }</style>"
        with pytest.raises(StarioError, match="would end the element early"):
            h.Style("</style><script>x()</script>")

    def test_raw_text_tags_still_escape_attributes(self):
        result = render(h.Script({"src": '/a.js?x="1"&y=2'}))
        assert result == '<script src="/a.js?x=&quot;1&quot;&amp;y=2"></script>'

    def test_script_safestring_children_are_verbatim(self):
        result = render(h.Script(SafeString("if (a < b) { go(); }")))
        assert result == "<script>if (a < b) { go(); }</script>"

    def test_raw_text_breakout_check_runs_over_the_joined_children(self):
        # The wire concatenates children, so a closing tag split across
        # two str children (or a list, or a nested list) is still a breakout.
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script("</scr", "ipt>")
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script(["</scr", "ipt>"])
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script("x = 1;", ["y", ["</scr", "ipt>"]])
        with pytest.raises(StarioError, match="would end the element early"):
            h.Style("</sty", "le>")

    def test_raw_text_safestring_separates_runs(self):
        # A SafeString is the caller's responsibility and sits between the
        # two str pieces on the wire, so "</scr" + SafeString + "ipt>" is
        # not a breakout.
        result = render(h.Script("</scr", SafeString("x"), "ipt>"))
        assert result == "<script></scrxipt></script>"


class TestRenderEdgeCases:
    def test_decimal_child_is_rejected(self):
        from decimal import Decimal

        with pytest.raises(StarioError, match="Cannot render element of type Decimal"):
            render(h.Span(cast(Any, Decimal("1.50"))))

    def test_render_rejects_arbitrary_object(self):
        class Widget:
            pass

        with pytest.raises(StarioError, match="Cannot render element of type Widget"):
            render(h.Div(cast(Any, Widget())))

    def test_render_list_as_root(self):
        assert render(cast(Any, [h.P("a"), h.P("b")])) == "<p>a</p><p>b</p>"

    def test_render_too_deep_raises_honest_error(self):
        node = h.Div("x")
        for _ in range(5000):
            node = h.Div(node)
        with pytest.raises(StarioError, match="nested too deeply to render"):
            render(node)

    def test_baked_too_deep_raises_honest_error(self):
        node = h.Div("x")
        for _ in range(5000):
            node = h.Div(node)
        deep_tree = node

        def builder():
            return deep_tree

        with pytest.raises(StarioError, match="nested too deeply to flatten"):
            baked(builder)
