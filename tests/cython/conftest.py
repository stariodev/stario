"""Load ``stario`` before Cython extensions.

Importing ``stario_cython.exchange`` while ``stario.__init__`` is still running
can re-enter the package. Completing the package init first makes collection
order-independent.
"""

import stario as _stario  # noqa: F401
from stario.testing import VirtualTimeLoop


def pytest_asyncio_loop_factories(config, item):
    """Run every protocol test on a clock that jumps over timer waits.

    Header, idle, body-stall, write-stall, lingering-close and GOAWAY
    deadlines all fire in virtual time, so a 5 s timeout costs milliseconds
    of wall time and the margins are exact instead of sleep-sized.
    """
    return {"virtual_time": VirtualTimeLoop}
