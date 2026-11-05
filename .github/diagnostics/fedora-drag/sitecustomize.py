"""Opt-in scratch-only sender trace for one native Fedora drag diagnostic.

Parent mounts this file as /diagnostics/sitecustomize.py and sets
PYTHONPATH=/diagnostics plus KWIN_MCP_DIAG_TRACE=/artifacts/sender-trace.tsv.
Only the installed kwin-mcp console process is instrumented. The probe and
pytest processes remain inert. Every real call and return value is preserved.
"""
from __future__ import annotations

import functools
import os
import sys
import time

trace_path = os.environ.get("KWIN_MCP_DIAG_TRACE")
if trace_path and os.path.basename(sys.argv[0]) == "kwin-mcp":
    import kwin_mcp.input as backend

    fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    pid = os.getpid()

    def record(name: str, boundary: str, detail: str = "") -> None:
        # One append write survives the server's os._exit path. Deliberately
        # avoid buffering, per-event fsync, stdout, and atexit callbacks.
        line = f"{time.monotonic_ns()}\t{pid}\t{name}\t{boundary}\t{detail}\n"
        os.write(fd, line.encode("utf-8"))

    def describe(args: tuple, kwargs: dict, *, method: bool) -> str:
        values = args[1:] if method else args
        # These hooks are pointer/timing only. Do not record app text, keyboard
        # payloads, credentials, arbitrary object reprs, or hold status messages.
        simple = tuple(value for value in values if isinstance(value, (int, float, bool)))
        named = tuple((key, value) for key, value in kwargs.items()
                      if isinstance(value, (int, float, bool)))
        return f"args={simple!r};kwargs={named!r}"

    def wrap(owner: object, name: str, *, method: bool, returns_timestamp: bool = False) -> None:
        descriptor = vars(owner).get(name)
        is_static = isinstance(descriptor, staticmethod)
        is_class = isinstance(descriptor, classmethod)
        original = descriptor.__func__ if is_static or is_class else getattr(owner, name)
        label = f"{getattr(owner, '__name__', type(owner).__name__)}.{name}"

        @functools.wraps(original)
        def traced(*args, **kwargs):
            record(label, "enter", describe(args, kwargs, method=method and not is_static))
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                record(label, "raise", type(error).__name__)
                raise
            record(label, "return", str(result) if returns_timestamp else "")
            return result

        replacement = staticmethod(traced) if is_static else classmethod(traced) if is_class else traced
        setattr(owner, name, replacement)

    record("trace", "start", f"module={backend.__file__};argv0={sys.argv[0]}")
    wrap(backend.InputBackend, "mouse_drag", method=True)
    wrap(backend, "_hold", method=False)
    wrap(backend, "_wait", method=False)
    wrap(backend.EISClient, "pointer_move_absolute", method=True)
    wrap(backend.EISClient, "pointer_button", method=True)
    wrap(backend.EISClient, "_now_us", method=True, returns_timestamp=True)
    wrap(backend.EISClient, "_flush", method=True)
    record("trace", "ready")
