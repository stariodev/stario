"""Markup rendering API for HTML and SVG trees.

Import the tag catalogs by alias and the helpers directly:

```python
from stario.markup import (
    html as h,
    svg,
    render,
    baked,
    classes,
    styles,
    data,
    aria,
)
```

**Catalogs:** `html`, `svg` — PascalCase tag factories (`h.Div`, `svg.Circle`, …).

**Helpers:** `render`, `baked`, `classes`, `styles`, `data`, `aria`.

**Constructors:** `Comment` — safe HTML comments.

**Types:** `HtmlElement`, `Element`, `SafeString`, `Attrs`, `Tag` (custom elements when
catalogs omit a name), `VoidTag` (custom void elements: attributes only).

**Bytes:** `render_bytes` — the same markup as `render`, UTF-8 encoded in one pass.

**Advanced types** (annotations): `from stario.markup.types import TagAttributes, AttributeValue`.

Custom `prefix-*` attributes beyond `data` / `aria`: import the internal
helper from `from stario.markup.attributes import prefixed`.

For Datastar attributes use `from stario.datastar import data`; `stario.markup.data`
is only for static `data-*` HTML attributes.

Lighter import paths when you do not need the full package:

- `import stario.markup.html as h` — tag catalog only (skips `baked`)
- `from stario.markup.render import render` — serialization only
"""

from . import html, svg
from .attributes import aria, classes, data, styles
from .baked import baked
from .render import render, render_bytes
from .tag import Tag, VoidTag
from .types import Attrs, Comment, Element, HtmlElement, SafeString

__all__ = [
    "Attrs",
    "Comment",
    "Element",
    "HtmlElement",
    "SafeString",
    "Tag",
    "VoidTag",
    "aria",
    "baked",
    "classes",
    "data",
    "html",
    "render",
    "render_bytes",
    "styles",
    "svg",
]
