"""Training run for a profile-guided build (``STARIO_PGO=1``, see setup.py).

Drives the request path and the markup builder through the in-process
benchmarks, so the compiler sees which branches a server takes. Runs
against the instrumented extensions setup.py puts first on PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def _load(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise ImportError(relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    inproc = _load("benchmarks/server/inproc.py", "_pgo_inproc")
    # Enough requests per case for stable branch counts; timings are unused.
    inproc.N = 4000
    inproc.REPEAT = 2
    asyncio.run(inproc.main(None))
    _load("benchmarks/html/micro.py", "_pgo_micro").main()


if __name__ == "__main__":
    main()
