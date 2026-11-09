"""The typed markup objects: sealed, immutable, checked at the call site."""

import copy
import gc
import inspect
import json
import pickle
import sys
import threading
import time
import uuid
from typing import Any, cast

import pytest

import stario.markup.escape as escape_module
from stario.datastar import data as ds
from stario.exceptions import StarioError
from stario.markup import (
    Attrs,
    Element,
    SafeString,
    Tag,
    VoidTag,
    baked,
    classes,
    data,
    render,
    render_bytes,
)
from stario.markup import html as h


class TestElementIsSealed:
    def test_python_code_cannot_create_an_element(self):
        for make in (
            lambda: Element(),  # type: ignore[call-arg]
            lambda: Element.__new__(Element),
            lambda: object.__new__(Element),
        ):
            # 3.13+ says "cannot create"; 3.12 says "object.__new__(...) is not safe".
            with pytest.raises(TypeError, match=r"cannot create|is not safe"):
                make()

    def test_element_cannot_be_subclassed(self):
        with pytest.raises(TypeError):
            type("Sub", (Element,), {})

    def test_fields_are_read_only(self):
        el = h.P({"id": "x"}, "hi")
        for name in ("tag", "attrs", "children", "name"):
            with pytest.raises(AttributeError):
                setattr(el, name, None)

    def test_a_hand_made_tuple_is_not_markup(self):
        # The old tuple form rendered these four strings as raw markup.
        forged = ("<img src=x onerror=alert(1)>", None, None, "")
        with pytest.raises(StarioError, match="Cannot render a tuple"):
            h.Div(cast(Any, forged))

    def test_copies_are_the_same_element(self):
        el = h.P("hi")
        assert copy.copy(el) is el
        assert copy.deepcopy(el) is el
        with pytest.raises(TypeError):
            pickle.dumps(el)


class TestElementReadApi:
    def test_named_fields(self):
        link = h.A({"href": "/u/1"}, classes("link"), "open", h.B("!"))
        assert link.tag is h.A
        assert link.name == "a"
        assert link.attrs == ' href="/u/1" class="link"'
        assert link.children[0] == "open"
        assert type(link.children[1]) is Element

    def test_children_are_a_tuple_without_the_attributes(self):
        # A None before the first child sits among the attributes and is skipped.
        el = h.Div({"id": "x"}, None, "a", None, ["b"])
        assert el.children == ("a", None, ["b"])
        assert h.Div("a", "b").children == ("a", "b")
        assert h.Div({"id": "x"}).children == ()

    def test_read_one_attribute(self):
        el = h.Input({"type": "text", "value": 'say "hi" & <go>', "required": True})
        assert el["type"] == "text"
        assert el["value"] == 'say "hi" & <go>'
        assert el["required"] is True
        assert el.get("missing") is None
        assert el.get("missing", "d") == "d"
        assert "type" in el
        assert "missing" not in el
        with pytest.raises(KeyError):
            el["missing"]

    def test_not_a_sequence(self):
        el = h.P("hi")
        with pytest.raises(TypeError, match="read by name"):
            el[0]  # type: ignore[index]
        with pytest.raises(TypeError, match="not iterable"):
            list(el)  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="not iterable"):
            h.Ul(*el)  # type: ignore[misc]

    def test_with_attrs_replaces_and_adds(self):
        el = h.A({"href": "/a", "id": "x"}, classes("link"), "open")
        changed = el.with_attrs({"href": "/b", "data-k": 1}, Attrs(' class="other"'))
        assert (
            render(changed) == '<a href="/b" id="x" class="other" data-k="1">open</a>'
        )
        assert render(el) == '<a href="/a" id="x" class="link">open</a>'
        assert changed.children is el.children

    def test_with_attrs_validates(self):
        with pytest.raises(StarioError, match="Invalid attribute name"):
            h.P("x").with_attrs({"bad key": "v"})
        with pytest.raises(StarioError, match="scalar values only"):
            h.P("x").with_attrs(cast(Any, {"k": ["v"]}))

    def test_with_attrs_removing_everything_gives_the_empty_element(self):
        assert h.Div({"hidden": True}).with_attrs({"hidden": False}) is h.Div.empty


class TestCallSiteChecks:
    @pytest.mark.parametrize(
        ("child", "message"),
        [
            (True, "Boolean values are not valid HTML child content"),
            (("a", "b"), "Cannot render a tuple as HTML content"),
            (h.Div, "Cannot render a Tag object directly"),
            (object(), "Cannot render element of type object"),
            (b"bytes", "Cannot render element of type bytes"),
        ],
    )
    def test_bad_children_raise_at_the_tag_call(self, child, message):
        with pytest.raises(StarioError, match=message):
            h.Div(child)

    def test_bad_items_inside_a_list_raise_at_render(self):
        el = h.Div(["ok", cast(Any, object())])
        with pytest.raises(StarioError, match="Cannot render element of type object"):
            render(el)

    def test_empty_call_returns_the_cached_element(self):
        assert h.Div() is h.Div.empty
        assert h.Div(None, {}) is h.Div.empty
        assert type(h.Br()) is Element
        assert render(h.Br(), h.Div()) == "<br/><div></div>"


class TestValueTypes:
    def test_safe_string_takes_only_str(self):
        with pytest.raises(TypeError):
            SafeString(cast(Any, 5))
        with pytest.raises(TypeError):
            SafeString(cast(Any, None))

    def test_attrs_must_start_with_a_space(self):
        with pytest.raises(StarioError, match="must start with a space"):
            Attrs('id="x"')
        assert render(h.Div(Attrs(""), "x")) == "<div>x</div>"

    def test_value_semantics(self):
        assert SafeString("a") == SafeString("a")
        assert hash(SafeString("a")) == hash(SafeString("a"))
        assert SafeString("a") != Attrs(" a")
        assert pickle.loads(pickle.dumps(SafeString("<b>"))) == SafeString("<b>")
        assert pickle.loads(pickle.dumps(Attrs(' id="x"'))) == Attrs(' id="x"')
        match SafeString("x"):
            case SafeString(rendered):
                assert rendered == "x"

    def test_final_types(self):
        for cls in (SafeString, Attrs):
            with pytest.raises(TypeError):
                type("Sub", (cls,), {})


class TestTags:
    def test_void_tags_are_void_tag(self):
        assert isinstance(h.Br, VoidTag)
        assert isinstance(h.Img, VoidTag)
        assert not isinstance(h.Div, VoidTag)
        assert render(VoidTag("x-icon")({"name": "a"})) == '<x-icon name="a"/>'
        with pytest.raises(StarioError, match="void element"):
            VoidTag("x-icon")(cast(Any, "child"))

    def test_a_tag_cannot_be_initialised_twice(self):
        with pytest.raises(TypeError, match="already initialised"):
            h.Div.__init__("span")  # type: ignore[misc]
        assert render(h.Div("x")) == "<div>x</div>"

    def test_an_uninitialised_tag_cannot_build(self):
        blank = Tag.__new__(Tag)
        with pytest.raises(TypeError, match="not initialised"):
            blank("x")


class TestRenderBytes:
    @pytest.mark.parametrize(
        "node",
        [
            h.P("plain"),
            h.P("a < b & c > d", None),
            h.P("Zażółć gęślą jaźń ✓ 😀 <&>"),
            h.Div(
                {"title": 'żółw ✓ "q"'}, SafeString("<b>ż</b>"), 7, 2.5, ["x", ["y"]]
            ),
            [h.P("a"), "b & c"],
        ],
    )
    def test_matches_render_encoded(self, node):
        assert render_bytes(node) == render(node).encode("utf-8")

    def test_large_output_grows_past_the_stack_buffer(self):
        page = h.Ul([h.Li(f"row {i} & more ✓") for i in range(5000)])
        assert render_bytes(page) == render(page).encode("utf-8")
        assert len(render_bytes(page)) > 100_000


class TestReferences:
    def test_a_cycle_through_a_list_is_collected(self):
        import weakref

        class Marker:
            pass

        marker = Marker()
        items: list[Any] = []
        outer = h.Ul(items, h.Li("x"))
        items.append(outer)  # outer -> children -> items -> outer
        items.append(marker)
        probe = weakref.ref(marker)
        del items, outer, marker
        gc.collect()
        assert probe() is None

    def test_attribute_dicts_are_not_kept(self):
        import weakref

        class D(dict):
            pass

        attrs = D(id="x")
        el = h.Div(attrs, "text")
        probe = weakref.ref(attrs)
        del attrs
        assert probe() is None
        assert render(el) == '<div id="x">text</div>'


class TestBakedStillWorks:
    def test_baked_with_typed_elements(self):
        @baked
        def row(label, href, active):
            return h.Li({"class": "row"}, h.A({"href": href, "data-on": active}, label))

        assert row("A & B", "/x?a=1&b=2", True) == SafeString(
            '<li class="row"><a href="/x?a=1&amp;b=2" data-on>A &amp; B</a></li>'
        )

    def test_baked_static_child_errors_keep_their_message(self):
        def bad():
            return h.Div(cast(Any, object()))

        with pytest.raises(StarioError, match="unsupported static child type object"):
            baked(bad)

    def test_baked_attribute_names_with_colons_and_braces(self):
        @baked
        def button(on_click, label, extra):
            return h.Button(
                {"data-on:click__debounce.500ms": on_click, "x{y}": extra}, label
            )

        assert button("@post('/a')", "Go {now}", 1) == SafeString(
            '<button data-on:click__debounce.500ms="@post(\'/a\')" x{y}="1">'
            "Go {now}</button>"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "quotes \" and ' both",
            "braces {x} {{y}} }{",
            "back\\slash \\n \\x41 \\N{BULLET}",
            "line\nbreak\r\ttab",
            "\u00e9t\u00e9 \u65e5\u672c \U0001f680 \u2028",
            '!r :>10 = f"{a!r}"',
        ],
    )
    def test_baked_static_text_goes_into_the_source_unchanged(self, text):
        # Static segments are string literals in the generated splice
        # function, inside f-string replacement fields.
        def build(value):
            return h.Div(
                {"title": text, "data-x": value},
                text,
                value,
                h.Script(f"var s = {json.dumps(text)};", value),
                text,
            )

        spliced = baked(build)
        for value in ("v", "{v}", 'a"b', "x\\y"):
            assert spliced(value) == SafeString(render(build(value)))

    def test_classes_cache_takes_none_and_false(self):
        from stario.markup import attributes

        room = len(attributes._CLASSES_CACHE) < attributes._CLASSES_CACHE_MAX
        first = classes("card-x9", None, False)
        assert first == Attrs(' class="card-x9"')
        if room:  # another test may have filled the shared cache to its cap
            assert classes("card-x9", None, False) is first
            assert ("card-x9", None, False) in attributes._CLASSES_CACHE
        assert classes("card-x9", {"on": True}) == Attrs(' class="card-x9 on"')


class TestThreads:
    def test_concurrent_build_and_render_match_a_single_thread(self):
        shared = h.Nav(classes("nav"), h.A({"href": "/"}, "Home"))  # shared, read-only

        def page(seed: int) -> Element:
            return h.Div(
                {"id": f"p{seed}"},
                shared,
                h.Ul(
                    [
                        h.Li(
                            classes("row", None, {"odd": i % 2 == 1}),
                            data({"i": i, "seed": seed}),
                            h.Span({"title": f't{i} & "q"'}, f"item {i} <{seed}>"),
                        )
                        for i in range(40)
                    ]
                ),
            )

        expected = {seed: render_bytes(page(seed)) for seed in range(8)}
        errors: list[BaseException] = []

        def worker(seed: int) -> None:
            try:
                for _ in range(300):
                    assert render_bytes(page(seed)) == expected[seed]
                    assert (
                        render(shared) == '<nav class="nav"><a href="/">Home</a></nav>'
                    )
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []

    def test_tags_made_on_many_threads_at_once(self):
        # Each new Tag (and its strings and empty element) is made immortal
        # on a free-threaded build while other threads build and render.
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(200):
                    tag = Tag(f"x-t{n}-{i}")
                    node = tag(classes(f"c{n}", "on"), str(i))
                    expected = f'<x-t{n}-{i} class="c{n} on">{i}</x-t{n}-{i}>'
                    assert render(node) == expected
                    assert render(tag()) == f"<x-t{n}-{i}></x-t{n}-{i}>"
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []


_FREE_THREADED = not getattr(sys, "_is_gil_enabled", lambda: True)()
_is_immortal = getattr(sys, "_is_immortal", None)


@pytest.mark.skipif(_is_immortal is None, reason="sys._is_immortal is 3.14+")
class TestSharedObjectsAreImmortal:
    """What every thread reads while building is immortal on free-threaded builds.

    Each element holds its Tag and each render reads the Tag's strings, so a
    refcount write there would be one cache line every core fights over.
    With the GIL nothing changes. A caller's own object is never made
    immortal (another thread may be changing its refcount): caches store an
    immortal copy instead.
    """

    def test_a_tag_and_what_it_holds(self):
        assert _is_immortal is not None
        tag = Tag("x-immortal-check")
        held = [
            tag,
            tag.empty,
            tag.tag_start,
            tag.tag_start_no_attrs,
            tag.closing_tag,
            tag.no_children_close,
            tag.empty_html,
        ]
        if _FREE_THREADED:
            assert all(_is_immortal(obj) for obj in held)
            assert all(_is_immortal(obj) for obj in (h.Div, h.Div.empty, h.Br))
        else:
            assert not _is_immortal(tag)
            assert not _is_immortal(tag.closing_tag)

    def test_a_cached_class_fragment(self):
        from stario.markup import attributes

        assert _is_immortal is not None
        tokens = ("imm-a", "".join(["imm-", "b"]))
        result = classes(*tokens)
        assert result == Attrs(' class="imm-a imm-b"')
        # Cached or not (another test may have filled the cache to its cap),
        # the caller's own tuple and strings stay mortal.
        assert not _is_immortal(tokens)
        assert not _is_immortal(tokens[1])
        if tokens in attributes._CLASSES_CACHE:
            assert classes("imm-a", "imm-b") is result
        # () is immortal on every build: look at a key with tokens in it.
        key, cached = next(kv for kv in attributes._CLASSES_CACHE.items() if kv[0])
        assert _is_immortal(cached) == _FREE_THREADED
        assert _is_immortal(cached.rendered) == _FREE_THREADED
        assert _is_immortal(key) == _FREE_THREADED

    def test_a_dynamic_attribute_name_is_copied_into_the_cache(self):
        assert _is_immortal is not None
        key = "".join(["data-", uuid.uuid4().hex])
        assert render(h.Div({key: "v"})) == f'<div {key}="v"></div>'
        assert render(h.Div(data({key: 1}))) == f'<div data-{key}="1"></div>'
        assert not _is_immortal(key)

    def test_a_static_baked_result_is_one_shared_object(self):
        assert _is_immortal is not None

        @baked
        def footer():
            return h.Footer(h.P("Made with Stario"))

        assert footer() is footer()
        assert footer() == SafeString("<footer><p>Made with Stario</p></footer>")
        assert _is_immortal(footer()) == _FREE_THREADED


class TestRawTextChecks:
    @pytest.mark.parametrize(
        "children",
        [
            ("</scr", None, "ipt><img src=x onerror=alert(1)>"),
            (["</scr", [None], "ipt>"],),
            ("</scr", SafeString(""), "ipt>"),
            ("<!", None, "--"),
        ],
    )
    def test_nothing_on_the_wire_never_splits_the_check(self, children):
        with pytest.raises(StarioError, match="would end the element early"):
            h.Script(*children)

    def test_style_too(self):
        with pytest.raises(StarioError, match="would end the element early"):
            h.Style("</sty", None, "le><script>alert(1)</script>")

    def test_numbers_join_the_text(self):
        assert render(h.Script("var n = ", 5, "; var f = ", 2.5, ";")) == (
            "<script>var n = 5; var f = 2.5;</script>"
        )

    def test_a_safe_string_is_the_callers_markup(self):
        assert render(h.Script(SafeString("<!-- legacy -->"))) == (
            "<script><!-- legacy --></script>"
        )

    def test_elements_are_not_raw_text(self):
        with pytest.raises(StarioError, match="holds raw text, not elements"):
            h.Script(h.Script("x"), "y")

    def test_the_tag_name_is_matched_in_any_case(self):
        style = Tag("STYLE", raw_text=True)
        with pytest.raises(StarioError, match="would end the element early"):
            style("</style><script>alert(1)</script>")

    def test_nesting_has_a_limit(self):
        cycle: list[Any] = []
        cycle.append(cycle)
        with pytest.raises(StarioError, match="nested too deeply"):
            h.Script(cycle)
        deep: list[Any] = ["x"]
        for _ in range(2000):
            deep = [deep]
        with pytest.raises(StarioError, match="nested too deeply"):
            h.Script(deep)

    def test_baked_parameters_around_none_are_checked_together(self):
        @baked
        def script(a, b):
            return h.Script(a, None, b)

        assert script("let a = 1;", "let b = 2;") == SafeString(
            "<script>let a = 1;let b = 2;</script>"
        )
        with pytest.raises(StarioError, match="would end the element early"):
            script("</scr", "ipt><img src=x onerror=alert(1)>")


class TestReadAttributesBack:
    def test_single_quoted_datastar_signals_stay_byte_for_byte(self):
        payload = {"q": '" onclick=alert(document.domain)//'}
        card = h.Div({"id": "card"}, ds.signals(payload), "body")
        changed = card.with_attrs({"class": "active"})
        assert render(changed) == render(card).replace(">body", ' class="active">body')
        assert "onclick" not in changed
        assert json.loads(cast(str, card["data-signals"])) == payload

    def test_values_are_decoded_like_a_browser(self):
        el = h.P({"t": SafeString("a&nbsp;b&#39;c"), "u": 'say "hi" & <go>'})
        assert el["t"] == "a\xa0b'c"
        assert el["u"] == 'say "hi" & <go>'
        assert render(el.with_attrs({"x": 1})) == render(el).replace(
            "></p>", ' x="1"></p>'
        )

    @pytest.mark.parametrize(
        ("fragment", "values", "rendered"),
        [
            (" a='x' b", {"a": "x", "b": True}, "<p a='x' b c=\"1\"></p>"),
            (" a=1 b=2", {"a": "1", "b": "2"}, '<p a=1 b=2 c="1"></p>'),
            (' a = "1"', {"a": "1"}, '<p a = "1" c="1"></p>'),
            ('\ta="1"\nb="2" ', {"a": "1", "b": "2"}, '<p a="1" b="2" c="1"></p>'),
        ],
    )
    def test_html_attribute_syntax(self, fragment, values, rendered):
        el = h.P(Attrs(fragment))
        for name, value in values.items():
            assert el[name] == value
        assert render(el.with_attrs({"c": 1})) == rendered

    def test_first_of_two_names_wins_and_a_replace_keeps_one(self):
        el = h.P({"a": "1"}, {"a": "2"})
        assert el["a"] == "1"
        assert render(el.with_attrs({})) == '<p a="1" a="2"></p>'
        assert render(el.with_attrs({"a": "3"})) == '<p a="3"></p>'
        assert el.with_attrs({"a": None}) is h.P.empty

    def test_unreadable_fragments_raise(self):
        el = h.P(Attrs(' a="x'))
        with pytest.raises(StarioError, match="no closing"):
            el["a"]
        with pytest.raises(StarioError, match="no closing"):
            el.with_attrs({"b": 1})

    def test_with_attrs_takes_mappings_and_attrs_only(self):
        with pytest.raises(StarioError, match="takes mappings and Attrs"):
            h.P("x").with_attrs(cast(Any, "class=x"))


class TestValueInputs:
    def test_str_subclasses_become_plain_str(self):
        class Markup(str):
            pass

        safe = SafeString(Markup("<b>x</b>"))
        assert type(safe.rendered) is str
        assert render(h.Div(safe)) == "<div><b>x</b></div>"
        assert type(Attrs(Markup(' id="x"')).rendered) is str

    def test_attrs_may_start_with_any_html_whitespace(self):
        assert render(h.Div(Attrs('\tid="x"'), "y")) == '<div\tid="x">y</div>'
        assert h.Div(Attrs("")) is h.Div.empty

    def test_type_errors_name_the_type(self):
        with pytest.raises(TypeError, match="SafeString takes a str, not int"):
            SafeString(cast(Any, 5))

    def test_signatures(self):
        assert str(inspect.signature(SafeString)) == "(rendered)"
        assert str(inspect.signature(Attrs)) == "(rendered)"
        tag = "(name, *, empty='normal', prefix='', raw_text=False)"
        assert str(inspect.signature(Tag)) == tag
        assert str(inspect.signature(Tag.__init__)) == tag.replace("(", "(self, /, ", 1)
        assert str(inspect.signature(Tag.__call__)) == "(self, /, *children)"
        assert str(inspect.signature(VoidTag)) == "(name, *, prefix='')"
        assert (
            str(inspect.signature(VoidTag.__init__)) == "(self, /, name, *, prefix='')"
        )

    def test_a_bad_child_error_names_the_parent(self):
        with pytest.raises(StarioError) as caught:
            h.Section(cast(Any, True))
        assert caught.value.context["parent"] == repr(h.Section)
        assert "child_kind" not in caught.value.context


class TestRenderErrors:
    def test_a_lone_surrogate_has_its_own_message(self):
        with pytest.raises(StarioError, match="lone surrogate") as caught:
            render(h.Div("a\ud800b"))
        assert caught.value.context["character"] == "U+D800"

    def test_long_text_renders_in_chunks(self):
        text = "ż & <x> " * 5000
        expected = (
            "<p>"
            + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</p>"
        )
        assert render(h.P(text)) == expected
        assert render_bytes(h.P(text)) == expected.encode()


class TestMappingsChangedDuringACall:
    """A new attribute name runs the Python validator, where another thread
    may run and change the mapping. Here the validator changes it itself."""

    @pytest.fixture
    def target(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        target: dict[str, Any] = {}
        real = escape_module._INVALID_KEY_CHARS

        class Window:
            def search(self, key: str) -> Any:
                target.clear()
                gc.collect()
                return real.search(key)

        monkeypatch.setattr(escape_module, "_INVALID_KEY_CHARS", Window())
        return target

    def test_one_pair_renders_the_value_it_read(self, target):
        key = f"k{uuid.uuid4().hex}"  # a new name misses every name cache
        target[key] = "".join(["value-", "x" * 40])
        assert render(h.Div(target)) == f'<div {key}="value-{"x" * 40}"></div>'

    @pytest.mark.parametrize("build", [h.Div, lambda d: h.Div(data(d))])
    def test_more_pairs_raise_cleanly(self, target, build):
        target["id"] = "x"
        target[f"k{uuid.uuid4().hex}"] = "".join(["value-", "x" * 40])
        with pytest.raises(RuntimeError, match="changed size during iteration"):
            build(target)


class TestSharedMutableChildren:
    """Another thread changes a list or dict in the tree: clean errors, no crash."""

    @staticmethod
    def _race(mutate, read, seconds=0.3):
        stop = time.monotonic() + seconds
        failures: list[str] = []

        def mutator():
            while time.monotonic() < stop:
                mutate()

        def reader():
            while time.monotonic() < stop:
                problem = read()
                if problem:
                    failures.append(problem)

        threads = [threading.Thread(target=mutator), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return failures

    def test_render_of_a_changing_list(self):
        shared: list[Any] = []
        el = h.Div(shared)

        def mutate():
            shared.extend([f"t{i}" for i in range(50)])
            shared.clear()

        def read():
            try:
                out = render(el)
            except StarioError:
                return None
            ok = out.startswith("<div>") and out.endswith("</div>")
            return None if ok else out[:80]

        assert self._race(mutate, read) == []

    def test_tag_call_with_a_changing_dict(self):
        shared: dict[str, Any] = {"id": "x"}

        def mutate():
            for j in range(20):
                shared[f"k{j}"] = "v"
            for j in range(20):
                shared.pop(f"k{j}", None)

        def read():
            try:
                out = render(h.Div(shared))
            except RuntimeError:
                return None
            ok = out.startswith('<div id="x"') and out.endswith("></div>")
            return None if ok else out[:80]

        assert self._race(mutate, read) == []
