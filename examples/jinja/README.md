# Jinja templates

An interactive counter shared across browser tabs, using Jinja templates,
Datastar, SSE, and an in-process Relay.

[`templates/home.html.jinja`](templates/home.html.jinja) renders the initial page
with `w.html()`. It includes
[`templates/counter.html.jinja`](templates/counter.html.jinja), which is also
rendered for each SSE update and sent through `SSE.patch_elements()`. The stable
`id="counter"` tells Datastar which element to update.

Jinja autoescaping is enabled, and `StrictUndefined` raises an error for missing
template variables. URLs inside Datastar expressions use Jinja's `tojson` filter
inside single-quoted HTML attributes.

## Run

```bash
git clone https://github.com/stariodev/stario.git
cd stario/examples/jinja
uv sync
uv run python -m main
```

Open http://127.0.0.1:8000 in two tabs. Click **Add one** or **Reset** in either
tab; both counters update without a page reload. **Show how it works** uses a
local Datastar signal and binding, so each tab keeps its own checkbox state.

The Datastar module loads from a pinned CDN URL and requires internet access.
The counter lives in one server process and resets when it restarts.

## Request flow

| Route | Purpose |
|-------|---------|
| `GET /` | Render the page and current counter |
| `GET /live` | Send the current counter, then stream Jinja fragments on Relay notifications |
| `POST /increment` | Increment the counter and notify subscribers; return 204 |
| `POST /reset` | Reset the counter and notify subscribers; return 204 |

The subscription stays on the outer `main` element while only the counter
fragment is patched. A reconnect receives the current count immediately.

See [hello-world](../hello-world/) for a minimal page or [Tiles](https://github.com/stariodev/tiles) for
a larger interactive app using Stario markup.

## Test

```bash
uv run pytest
```

The tests cover updates to two subscribers, reset, reconnect, initial page state,
and template escaping.
