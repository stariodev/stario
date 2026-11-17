"""Deprecated: `debug_inspector` lives in `stario.datastar`. Removed in 6.0.

```python
from stario.datastar import debug_inspector
```
"""

import warnings

from stario.datastar.debug import debug_inspector

# Kept for 5.x so `from stario.debug import debug_inspector` still works; the
# inspector shows Datastar signals, so its home is stario.datastar.
warnings.warn(
    "stario.debug is deprecated: import debug_inspector from stario.datastar. "
    "Removed in 6.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["debug_inspector"]
