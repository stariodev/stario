"""Small HTML tag helpers for loading Datastar."""

from stario.markup import HtmlElement
from stario.markup import html as h

DATASTAR_CDN_URL = (
    "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.5/bundles/datastar.js"
)


def ModuleScript(src: str = DATASTAR_CDN_URL) -> HtmlElement:
    """Load the Datastar client as a module script.

    ```python
    from stario.datastar import ModuleScript
    from stario.markup import html as h

    h.Head(ModuleScript())
    ```
    """
    return h.Script({"type": "module", "src": src})
