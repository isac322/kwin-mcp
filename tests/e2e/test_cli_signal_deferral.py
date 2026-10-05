"""Thread-independence regression for the CLI's shutdown-signal deferral.

``kwin_mcp.cli`` defers SIGTERM/SIGHUP/SIGINT while the session is being torn down so a
signal cannot interrupt the app-kill loop (``LiveSession.stop`` clears its running flag
before terminating the apps) and skip the remaining apps. A process-directed signal can
be delivered to any thread, so the deferral must not depend on which thread receives it.
This test drives the deferral directly (no KWin session): inside the teardown context it
delivers SIGTERM both process-directed and to the main thread while a second non-daemon
thread exists, and asserts the signal is deferred (no KeyboardInterrupt inside the body)
and then raised as exactly one on context exit.
"""

from __future__ import annotations

import os
import signal
import threading

import kwin_mcp.cli as cli

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _helper_thread_target(stop: threading.Event) -> None:
    """A long-lived non-daemon thread so a process-directed signal has another recipient."""
    stop.wait(timeout=30)


def test_defer_shutdown_signals_is_thread_independent() -> None:
    """A signal delivered to any thread is deferred during teardown, then raised once."""
    # Save and reset the deferral state (a previous test may have left it dirty).
    saved_depth = cli._teardown_depth
    saved_pending = cli._pending_shutdown_signal
    cli._teardown_depth = 0
    cli._pending_shutdown_signal = None

    previous_handlers = {signum: signal.getsignal(signum) for signum in _SHUTDOWN_SIGNALS}
    for signum in _SHUTDOWN_SIGNALS:
        signal.signal(signum, cli._on_shutdown_signal)

    stop = threading.Event()
    helper = threading.Thread(target=_helper_thread_target, args=(stop,), daemon=False)
    helper.start()
    body_completed = False
    raised = False
    try:
        with cli._defer_shutdown_signals():
            # Process-directed (can land on the helper thread) and main-thread-directed.
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            # Reaching here means no KeyboardInterrupt escaped the teardown body.
            body_completed = True
    except KeyboardInterrupt:
        raised = True
    finally:
        # Clean up: stop the helper thread, restore the handlers, reset the state.
        stop.set()
        helper.join(timeout=5)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        cli._teardown_depth = saved_depth
        cli._pending_shutdown_signal = saved_pending

    assert body_completed, "a KeyboardInterrupt escaped the teardown body (signal not deferred)"
    assert raised, "the deferred signal was not raised on teardown context exit"


def test_shutdown_signal_outside_teardown_raises() -> None:
    """A shutdown signal outside the teardown context raises KeyboardInterrupt immediately."""
    saved_depth = cli._teardown_depth
    saved_pending = cli._pending_shutdown_signal
    cli._teardown_depth = 0
    cli._pending_shutdown_signal = None

    previous_handlers = {signum: signal.getsignal(signum) for signum in _SHUTDOWN_SIGNALS}
    for signum in _SHUTDOWN_SIGNALS:
        signal.signal(signum, cli._on_shutdown_signal)

    raised = False
    try:
        # No teardown context: the handler must raise KeyboardInterrupt directly.
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
    except KeyboardInterrupt:
        raised = True
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        cli._teardown_depth = saved_depth
        cli._pending_shutdown_signal = saved_pending

    assert raised, "a shutdown signal outside teardown must raise KeyboardInterrupt"
