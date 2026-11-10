"""Regressions from the second review pass over markup and Datastar."""

from html.parser import HTMLParser

import pytest

from stario.datastar import DatastarAttributes, at, data
from stario.exceptions import StarioError
from stario.markup import html as h
from stario.markup import render


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
