# Stario examples

Runnable reference apps for Stario. Clone the [stario repository](https://github.com/stariodev/stario) and run one locally, or copy an example directory into your own project. Each example runs with `uv run python -m …` — `serve()` is the only server entry point.

| Example | Size | Start here? |
|---------|------|-------------|
| **[Tiles](https://github.com/stariodev/tiles)** | Its own repository | Yes — collaborative board, SSE + Relay |
| **[hello-world](hello-world/)** | Single `main.py` | One route, one HTML page |
| **[jinja](jinja/)** | `main.py` + Jinja templates | Shared counter with live SSE updates |
| **[chat-room](chat-room/)** | Multi-file `app/features/*` | Larger layout with SQLite and tests |

## Quick start (Tiles)

Tiles moved to its own repository, [stariodev/tiles](https://github.com/stariodev/tiles), where it runs as a full project with tests and deploys on Yard:

```bash
git clone https://github.com/stariodev/tiles
cd tiles
uv sync
uv run python -m app.main
```

Open http://127.0.0.1:8000 — paint in two tabs to see live sync. The [Realtime tiles tutorial](https://stario.dev/docs/tutorials/realtime-tiles) builds it in one file.
