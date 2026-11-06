"""Hypothesis profiles: deterministic on CI, the defaults elsewhere."""

import os

from hypothesis import settings

settings.register_profile("ci", derandomize=True, deadline=None)
settings.register_profile("dev")
settings.load_profile(
    "ci" if os.environ.get("CI", "").lower() not in {"", "0", "false"} else "dev"
)
