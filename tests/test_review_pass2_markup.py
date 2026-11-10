"""Regressions from the second review pass over markup and Datastar."""

import ctypes
from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stario.datastar import DatastarAttributes, at, data
from stario.exceptions import StarioError
from stario.markup import Attrs, SafeString, render, render_bytes
from stario.markup import data as mdata
from stario.markup import html as h
from stario.markup.escape import escape_attribute_value


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, attrs))


def _attr_names(fragment) -> list[str]:
    parser = _Collect()
    parser.feed(render(h.Div(fragment)))
    parser.close()
    assert len(parser.tags) == 1
    return [name for name, _ in parser.tags[0][1]]


# ---------------------------------------------------------------------------
# Datastar attribute names: every string that lands in the attribute NAME is
# checked like an attribute key, so it cannot start another attribute.

INJECT = "x onfocus=alert(1) autofocus"


@pytest.mark.parametrize(
    "build",
    [
        lambda: data.on("click", "go()", target=INJECT),  # type: ignore[arg-type]
        lambda: data.on("click", "go()", case=INJECT),  # type: ignore[arg-type]
        lambda: data.on_intersect("go()", threshold=INJECT),
        lambda: data.scroll_into_view(behavior=INJECT),  # type: ignore[arg-type]
        lambda: data.scroll_into_view(horizontal=INJECT),  # type: ignore[arg-type]
        lambda: data.scroll_into_view(vertical=INJECT),  # type: ignore[arg-type]
        lambda: DatastarAttributes("data- onfocus=alert(1) x-").text("$x"),
    ],
)
def test_datastar_attribute_name_parts_cannot_inject_attributes(build):
    with pytest.raises(StarioError):
        build()


def test_datastar_attribute_name_parts_still_accept_documented_values():
    assert _attr_names(data.on("click", "go()", target="window")) == [
        "data-on:click__window"
    ]
    assert _attr_names(data.on("myEvent", "go()", case="snake")) == [
        "data-on:my-event__case.snake"
    ]
    assert _attr_names(data.on_intersect("go()", threshold="half")) == [
        "data-on-intersect__half"
    ]
    assert _attr_names(data.on_intersect("go()", threshold="25")) == [
        "data-on-intersect__threshold.25"
    ]
    assert _attr_names(
        data.scroll_into_view(behavior="smooth", horizontal="start", vertical="end")
    ) == ["data-scroll-into-view__smooth__hstart__vend"]
    assert _attr_names(DatastarAttributes("data-star").text("$x")) == ["data-star-text"]


# ---------------------------------------------------------------------------
# Datastar action options that are numbers or booleans in the JS object:
# a str there was pasted in as JavaScript.


@pytest.mark.parametrize(
    "option",
    ["retry_interval_ms", "retry_max_wait_ms", "retry_max_count", "retry_scaler"],
)
def test_fetch_numeric_options_reject_non_numbers(option):
    with pytest.raises(StarioError):
        at.get("/x", **{option: "1}); alert(1); ({"})  # type: ignore[arg-type]
    with pytest.raises(StarioError):
        at.get("/x", **{option: True})  # type: ignore[arg-type]


def test_fetch_numeric_options_reject_non_finite_scaler():
    with pytest.raises(StarioError):
        at.get("/x", retry_scaler=float("nan"))
    with pytest.raises(StarioError):
        at.get("/x", retry_scaler=float("inf"))


def test_fetch_numeric_options_keep_their_output():
    action = at.get(
        "/x",
        retry_interval_ms=100,
        retry_scaler=1.5,
        retry_max_wait_ms=2000,
        retry_max_count=3,
        open_when_hidden=True,
    )
    assert action == (
        "@get('/x', {openWhenHidden: true, retryInterval: 100, retryScaler: 1.5, "
        "retryMaxWait: 2000, retryMaxCount: 3})"
    )


def test_fetch_open_when_hidden_is_a_boolean_literal():
    action = at.get("/x", open_when_hidden="true); alert(1); (1")  # type: ignore[arg-type]
    assert "alert" not in action


def test_fit_rejects_non_number_bounds():
    with pytest.raises(StarioError):
        at.fit("$x", "0); alert(1); (0", 1, 0, 1)  # type: ignore[arg-type]
    assert at.fit("$x", 0, 1.5, 0, 100) == "@fit($x, 0, 1.5, 0, 100, false, false)"


# ---------------------------------------------------------------------------
# @intl options: every str value is a string literal, at any depth (the
# docstring's promise); nested mappings used to paste nested strings as code.


def test_intl_nested_option_strings_are_literals():
    action = at.intl("number", "$x", {"a": {"b": "alert(1)"}, "c": ["alert(2)"]})
    assert action == "@intl('number', $x, {'a':{'b':'alert(1)'},'c':['alert(2)']})"


# ---------------------------------------------------------------------------
# The render buffer escapes ASCII text in runs, encodes text that is not
# ASCII itself (or copies the UTF-8 a str already carries), and builds ASCII
# output without a UTF-8 decode. Pin all of it against a reference, across
# string kinds, run boundaries, and chunks.


def test_long_class_lists_are_not_cached_forever():
    # Cache entries are never freed (immortal on free-threaded builds), so a
    # class token from user data used to pin its size per entry: 1024 calls
    # with 1 MB tokens kept ~2 GB alive after every reference was dropped.
    from stario.markup import attributes, classes

    token = "u" * 5000
    before = len(attributes._CLASSES_CACHE)
    assert classes(token, "x") == Attrs(f' class="{token} x"')
    assert len(attributes._CLASSES_CACHE) == before
    assert (token, "x") not in attributes._CLASSES_CACHE
    # Short lists are still cached.
    assert classes("pass2-short", "x") is classes("pass2-short", "x")


def test_long_attribute_names_are_not_cached_forever():
    from stario.markup.escape import _validate_attribute_key_cached
    from stario_cython.markup import _valid_key_cache_size

    key = "data-" + "k" * 5000
    mdata({"warm": "v"})  # caches the short prefix name itself
    size = _valid_key_cache_size()
    hits = _validate_attribute_key_cached.cache_info().currsize
    assert render(h.Div({key: "v"})) == f'<div {key}="v"></div>'
    assert render(h.Div(mdata({"k" * 5000: "v"}))) == f'<div {key}="v"></div>'
    assert _valid_key_cache_size() == size
    assert _validate_attribute_key_cached.cache_info().currsize == hits
    with pytest.raises(StarioError, match="Invalid attribute name"):
        h.Div({key + " x": "v"})


def _cache_utf8(text: str) -> None:
    get = ctypes.pythonapi.PyUnicode_AsUTF8AndSize
    get.restype = ctypes.c_char_p
    get.argtypes = [ctypes.py_object, ctypes.c_void_p]
    assert get(text, None) is not None


def _reference_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEXT_ALPHABET = st.characters(exclude_categories=["Cs"]) | st.sampled_from(
    list("<>&\"'=;\x00\x7f\x80\xff߿ࠀ￿\U00010000\U0010ffff")
)


@settings(max_examples=500)
@given(st.text(_TEXT_ALPHABET, max_size=60), st.text(_TEXT_ALPHABET, max_size=20))
def test_render_text_and_attrs_match_reference(text, value):
    el = h.P({"title": value}, text, SafeString(value), h.B(text))
    expected = (
        f'<p title="{escape_attribute_value(value)}">{_reference_text(text)}'
        f"{value}<b>{_reference_text(text)}</b></p>"
    )
    assert render(el) == expected
    assert render_bytes(el) == expected.encode()
    # Again, with the UTF-8 copy CPython caches on a str (used when present).
    _cache_utf8(text)
    _cache_utf8(value)
    assert render(el) == expected
    assert render_bytes(el) == expected.encode()


@pytest.mark.parametrize(
    "char",
    ["a", "&", "\xe9", "€", "\U0001f600"],
    ids=["ascii", "amp", "latin1", "bmp", "astral"],
)
def test_render_long_text_crosses_chunks(char):
    text = ("x" + char) * 9000 + "<" + char * 5000
    expected = _reference_text(text)
    assert render(h.Div(text)) == f"<div>{expected}</div>"
    assert render(SafeString(text)) == text
    assert render_bytes(h.Div({"title": text})) == (
        f'<div title="{escape_attribute_value(text)}"></div>'.encode()
    )


@pytest.mark.parametrize("prefix", ["", "\xe9", "€", "\U0001f600", "x" * 9000])
def test_render_lone_surrogate_still_raises(prefix):
    for text in (prefix + "\ud800", prefix + "\udfff tail"):
        with pytest.raises(StarioError, match="lone surrogate"):
            render(h.P(text))
        with pytest.raises(StarioError, match="lone surrogate"):
            render_bytes(SafeString(text))
