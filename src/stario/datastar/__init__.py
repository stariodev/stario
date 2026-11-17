"""Datastar helpers for attributes, actions, tags, signals, and SSE.

Import the default singletons and the classes that build them:

```python
from stario.datastar import (
    SSE,
    Debounce,
    Throttle,
    at,
    data,
    is_datastar,
    literal,
    read_signals,
)
```

`ModuleScript()` loads the Datastar client; `DebugInspector()` shows the
page's live signals while you develop.

`at` and `data` are the stock `DatastarActions` / `DatastarAttributes`
instances. Import the classes when you need a separate namespace (tests or
multiple configured builders).
"""

from .actions import DatastarActions, at
from .attributes import DatastarAttributes, data
from .debug import DebugInspector
from .format import Debounce, Duration, Throttle, literal
from .signals import FileSignal, is_datastar, read_signals
from .sse import SSE
from .tags import DATASTAR_CDN_URL, ModuleScript

__all__ = [
    "DATASTAR_CDN_URL",
    "SSE",
    "DatastarActions",
    "DatastarAttributes",
    "Debounce",
    "DebugInspector",
    "Duration",
    "FileSignal",
    "ModuleScript",
    "Throttle",
    "at",
    "data",
    "is_datastar",
    "literal",
    "read_signals",
]
