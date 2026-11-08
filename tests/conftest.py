"""Hypothesis profiles (deterministic on CI) and per-test signal hygiene."""

import os
import signal
import sys

import pytest
from hypothesis import settings

settings.register_profile("ci", derandomize=True, deadline=None)
settings.register_profile("dev")
settings.load_profile(
    "ci" if os.environ.get("CI", "").lower() not in {"", "0", "false"} else "dev"
)


if sys.platform == "win32":
    _SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK)
else:
    _SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """A server that shut down leaves SIG_IGN installed (by design, for the
    rest of the process). Put the handlers back so later tests start clean
    and Ctrl+C still stops pytest."""
    saved = {sig: signal.getsignal(sig) for sig in _SHUTDOWN_SIGNALS}
    yield
    for sig, handler in saved.items():
        if handler is not None:
            signal.signal(sig, handler)
