from collections.abc import Iterable
from typing import Any, Literal

from stario.markup.types import Element, HtmlElement, SafeString, TagAttributes

type EmptyMode = Literal["normal", "void", "self_closing_when_empty"]

def escape_text(s: str) -> str: ...
def escape_attribute_value(s: str) -> str: ...
def check_raw_text(text: str, tag: str) -> None: ...

class Tag:
    name: str
    empty_mode: str
    raw_text: bool
    tag_start: str
    tag_start_no_attrs: str
    closing_tag: str
    no_children_close: str
    empty: SafeString
    def __init__(
        self,
        name: str,
        *,
        empty: EmptyMode = "normal",
        prefix: str = "",
        raw_text: bool = False,
    ) -> None: ...
    def __call__(
        self, *children: TagAttributes | HtmlElement | None
    ) -> Element | SafeString: ...
    def _repr(self) -> str: ...

def render_nodes_to_string(
    nodes: Iterable[Any], *, root_count: int | None = None
) -> str: ...
def render(*nodes: HtmlElement) -> str: ...
