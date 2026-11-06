"""Tag objects that build HTML trees.

`Tag` is implemented in `stario_cython.markup`: one type switch per child,
one dict lookup per validated attribute name, escaping in one C pass. This
module keeps the public name and the type aliases.
"""

from typing import Literal

from stario_cython.markup import Tag

type EmptyMode = Literal["normal", "void", "self_closing_when_empty"]

__all__ = ["EmptyMode", "Tag"]
