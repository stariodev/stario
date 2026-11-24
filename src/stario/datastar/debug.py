"""`debug_inspector()`: the page's live Datastar signals, for local development."""

# Import from the module, not the package: stario.datastar imports this file.
from stario.datastar.attributes import data
from stario.markup import HtmlElement, SafeString, baked
from stario.markup import html as h

# Hard yellow sticker: obvious debug chrome, not product UI.
_CSS = """
#__stario_debug_inspector {
  position: fixed;
  z-index: 2147483647;
  right: max(0.75rem, env(safe-area-inset-right));
  bottom: max(0.75rem, env(safe-area-inset-bottom));
  max-width: calc(100vw - 1.5rem);
  color: #111;
  font: 700 0.72rem/1 ui-sans-serif, system-ui, sans-serif;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  box-shadow: 3px 3px 0 #111;

  header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 40px;
    padding-left: 0.65rem;
    border: 2px solid #111;
    border-radius: 2px;
    background: #ffe14a;
  }

  .drag {
    flex: 1;
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .drag:active {
    cursor: grabbing;
  }

  .tag {
    display: inline-block;
    padding: 0.15rem 0.3rem;
    border: 1.5px solid #111;
    background: #111;
    color: #ffe14a;
    margin-right: 0.35rem;
  }

  header button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    align-self: stretch;
    min-width: 40px;
    margin: -2px -2px -2px 0;
    border: 0;
    border-left: 2px solid #111;
    background: transparent;
    color: inherit;
    font: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    cursor: pointer;
  }

  section {
    width: min(320px, calc(100vw - 1.5rem));
    border: 2px solid #111;
    border-top: 0;
    border-radius: 0 0 2px 2px;
    background: #fffdf2;
  }

  pre {
    margin: 0;
    padding: 0.7rem;
    background: #fff;
    color: #111;
    font: 400 0.75rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: normal;
    text-transform: none;
    max-height: min(50dvh, 320px);
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}
"""

_INIT = """
el._clamp = () => {
  if (!(el.style.left || el.style.top)) return;
  const r = el.getBoundingClientRect();
  el.style.left = Math.max(8, Math.min(r.left, innerWidth - r.width - 8)) + 'px';
  el.style.top = Math.max(8, Math.min(r.top, innerHeight - r.height - 8)) + 'px';
};
el.addEventListener('pointerdown', (evt) => {
  if (evt.button !== 0 || !evt.target.closest('.drag')) return;
  const rect = el.getBoundingClientRect();
  el._drag = { id: evt.pointerId, ox: evt.clientX - rect.left, oy: evt.clientY - rect.top };
  el.setPointerCapture(evt.pointerId);
});
el.addEventListener('pointermove', (evt) => {
  if (!el._drag || el._drag.id !== evt.pointerId) return;
  const rect = el.getBoundingClientRect();
  el.style.left = Math.max(8, Math.min(evt.clientX - el._drag.ox, innerWidth - rect.width - 8)) + 'px';
  el.style.top = Math.max(8, Math.min(evt.clientY - el._drag.oy, innerHeight - rect.height - 8)) + 'px';
  el.style.right = el.style.bottom = 'auto';
});
const endDrag = (evt) => {
  if (!el._drag || el._drag.id !== evt.pointerId) return;
  try { el.releasePointerCapture(evt.pointerId); } catch {}
  el._drag = null;
  el._clamp();
};
el.addEventListener('pointerup', endDrag);
el.addEventListener('pointercancel', endDrag);
window.addEventListener('resize', () => el._clamp());
"""


@baked
def debug_inspector() -> HtmlElement:
    """Draggable overlay showing live Datastar signal JSON.

    Opt-in: place the element inside the page body when you want it.

    ```python
    from stario.datastar import debug_inspector

    h.Body(..., debug_inspector())
    ```
    """
    return h.Div(
        {"id": "__stario_debug_inspector"},
        data.ignore_morph(),
        data.signals({"_stario_debug_open": True}, if_missing=True),
        data.init(_INIT),
        h.Style(SafeString(_CSS)),
        h.Header(
            h.Span(
                {"class": "drag", "title": "Drag to move"},
                h.Span({"class": "tag"}, "dbg"),
                "signals",
            ),
            h.Button(
                {"type": "button", "aria-label": "Toggle signals inspector"},
                data.on("click", "$_stario_debug_open = !$_stario_debug_open"),
                data.text("$_stario_debug_open ? '▼' : '▲'"),
            ),
        ),
        h.Section(
            data.show("$_stario_debug_open"),
            h.Pre(data.json_signals(exclude=["_stario_debug_open"])),
        ),
    )
