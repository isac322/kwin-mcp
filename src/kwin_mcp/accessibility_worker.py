"""AT-SPI Pool worker entry points.

Module-top imports MUST stay stdlib-only. ``gi.repository.Atspi`` caches
its D-Bus connection process-wide; if this module pulled it in at import
time, the parent (which imports this module to grab the Pool initializer
+ dispatcher callables) would taint every later worker. Both functions
therefore defer all gi / accessibility imports into their own bodies.
"""

from __future__ import annotations

import logging
import os
import signal
from typing import Any

logger = logging.getLogger("kwin_mcp.atspi")

# Bound AT-SPI startup so a stale registry cannot hang worker creation forever.
_ATSPI_INIT_TIMEOUT_SEC = 5


def _init_atspi_worker(dbus_address: str) -> None:
    """Pool initializer (spawn context).

    Pins ``DBUS_SESSION_BUS_ADDRESS`` for the worker process so AT-SPI
    discovery hits the isolated session, then forces Atspi to bind by
    reading the desktop's child count. ``-1`` means the registry is
    unreachable; surface that as a clear init error.
    """
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_address

    def _alarm_handler(_signum: int, _frame: object) -> None:
        raise RuntimeError(
            "AT-SPI worker init failed: "
            f"timed out after {_ATSPI_INIT_TIMEOUT_SEC}s (stale registry?) "
            f"(DBUS={dbus_address})"
        )

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(_ATSPI_INIT_TIMEOUT_SEC)
    try:
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is None:
            raise RuntimeError(f"AT-SPI worker init failed: desktop is None (DBUS={dbus_address})")
        count = desktop.get_child_count()
        if count < 0:
            raise RuntimeError(
                f"AT-SPI worker init failed: child_count={count} (DBUS={dbus_address})"
            )
    finally:
        signal.alarm(0)
    logger.info("AT-SPI worker initialized for bus %s (child_count=%d)", dbus_address, count)


def do_atspi_op(op: str, **kwargs: Any) -> dict[str, Any]:
    """Worker dispatcher.

    Delegates to ``kwin_mcp.accessibility._handle_request`` so the same
    op handlers serve both the standalone ``__main__`` debug entry and
    the Pool path. Imported here, never at module top.
    """
    from kwin_mcp.accessibility import _handle_request

    request: dict[str, Any] = {"op": op, **kwargs}
    return _handle_request(request)
