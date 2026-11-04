"""Compiled HTTP core for Stario: llhttp and nghttp2 behind one asyncio protocol.

Import the public types from ``stario`` (``stario.http.*``). This package is
the implementation; ``stario.serve()`` is the only server entry point.
"""

# The extensions import stario at module init and stario imports them back;
# loading stario first keeps `import stario_cython.exchange` order-independent.
import stario  # noqa: F401
