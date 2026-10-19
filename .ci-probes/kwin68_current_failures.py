"""Temporary observation-only hooks for the current KWin 6.8 failure nodes.

This module is loaded with ``-p`` from outside the image's test tree.  It wraps
only public test-side calls, records the original return/exception unchanged,
and never turns an error into a passing result.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import os
from pathlib import Path
import subprocess
import time
import traceback
from typing import Any

OUT = Path(os.environ.get("KWIN_MCP_ARTIFACT_DIR", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
_EVENTS = OUT / "current-failure-observations.jsonl"
_PATCHED = False
_CLI_PATCHED = False
_CAPTURING = False
_ENGINES: list[Any] = []

_SAFE_ENV = {
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "QT_ACCESSIBILITY",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
    "KWIN_MCP_X11_SCREENSHOT",
    "KWIN_MCP_E2E_STRICT_SKIPS",
    "KWIN_MCP_ARTIFACT_DIR",
    "KWIN_MCP_PROBE_EVENT_LOG",
    "AT_SPI_BUS_ADDRESS",
    "DBUS_SESSION_BUS_ADDRESS",
}
_SECRET_WORDS = ("token", "password", "secret", "credential", "private_key", "authorization")


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _json(value.model_dump(mode="json", by_alias=True, exclude_none=True))
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {str(key): _json(item) for key, item in vars(value).items() if not key.startswith("_")}
    return repr(value)


def _safe_args(value: Any, key: str = "") -> Any:
    if any(word in key.lower() for word in _SECRET_WORDS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _safe_args(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_args(item, key) for item in value]
    return _json(value)


def _write(name: str, value: Any) -> None:
    (OUT / name).write_text(json.dumps(_json(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _event(kind: str, **payload: Any) -> None:
    with _EVENTS.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.time(), "kind": kind, **_json(payload)}) + "\n")


def _selected_environment() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(_SAFE_ENV):
        if key not in os.environ:
            continue
        value = os.environ[key]
        if key.endswith("ADDRESS"):
            result[key] = {"present": True, "sha256": hashlib.sha256(value.encode()).hexdigest()}
        else:
            result[key] = value
    return result


def _run(name: str, argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        payload = {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-10000:],
        }
    except Exception as error:
        payload = {"argv": argv, "error": {"type": type(error).__name__, "message": str(error)}}
    _write(f"{name}.json", payload)
    return payload


def _session(engine: Any) -> Any:
    try:
        info = engine.session_info().session
    except Exception:
        return None
    return info


def _probe_context() -> dict[str, Any]:
    for frame_info in inspect.stack()[2:]:
        for value in frame_info.frame.f_locals.values():
            if hasattr(value, "window_id") and hasattr(value, "origin"):
                return {
                    "window_id": getattr(value, "window_id", None),
                    "origin": _json(getattr(value, "origin", None)),
                    "pid": getattr(value, "pid", None),
                    "log_path": str(getattr(value, "log_path", "")),
                }
    return {}


def _external_frame(label: str, payload: dict[str, Any]) -> None:
    target = OUT / f"frame-{label}.png"
    env = {key: value for key, value in os.environ.items()
           if key not in {"GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_STEP_DEBUG", "GH_DEBUG"}}
    for name, command in (("scrot", ["scrot", "-o", str(target)]),
                          ("grim", ["grim", str(target)])):
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=8, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            payload[f"{name}_frame"] = {"error": type(error).__name__}
            continue
        if result.returncode == 0 and target.is_file():
            payload["frame_copy"] = {
                "source": name, "copy": str(target),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "bytes": target.stat().st_size,
            }
            return
        payload[f"{name}_frame"] = {
            "returncode": result.returncode, "stderr": result.stderr[-2000:]}


def _capture_state(label: str, engine: Any | None = None, *, args: Any = None) -> None:
    global _CAPTURING
    if _CAPTURING:
        return
    _CAPTURING = True
    try:
        payload: dict[str, Any] = {
            "label": label,
            "environment": _selected_environment(),
            "request": _safe_args(args),
            "probe": _probe_context(),
        }
        _run(f"processes-{label}", ["ps", "-eo", "pid,ppid,user,stat,etime,args"])
        payload["process_snapshot"] = f"processes-{label}.json"
        session = _session(engine) if engine is not None else None
        address = getattr(session, "dbus_address", None) or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        payload["session"] = _json(session)
        if address:
            _run(
                f"a11y-get-address-{label}",
                ["gdbus", "call", "--address", address, "--dest", "org.a11y.Bus",
                 "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"],
                timeout=8,
            )
            try:
                from kwin_oracle import KWinOracle

                payload["windows"] = [_json(window) for window in KWinOracle(address).windows()]
            except Exception as error:
                payload["windows_error"] = {"type": type(error).__name__, "message": str(error)}
            try:
                from atspi_oracle import AtspiOracle

                payload["accessibility_tree"] = [_json(node) for node in AtspiOracle(address, timeout_s=8).walk(max_depth=20)]
            except Exception as error:
                payload["accessibility_tree_error"] = {"type": type(error).__name__, "message": str(error)}
        if engine is not None:
            try:
                frame = engine.screen_capture(max_edge=0)
                payload["frame"] = _json(frame)
                source = Path(frame.path)
                if source.is_file():
                    target = OUT / f"frame-{label}.png"
                    target.write_bytes(source.read_bytes())
                    payload["frame_copy"] = {
                        "source": str(source),
                        "copy": str(target),
                        "buffer_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "buffer_bytes": target.stat().st_size,
                    }
            except Exception as error:
                payload["frame_error"] = {"type": type(error).__name__, "message": str(error)}
        else:
            _external_frame(label, payload)
        _write(f"state-{label}.json", payload)

        _event("state", **payload)
    finally:
        _CAPTURING = False
def _mcp_unsatisfied(result: Any) -> bool:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        wait = structured.get("wait")
        if isinstance(wait, dict) and wait.get("satisfied") is False:
            return True
    content = getattr(result, "content", ())
    return any("NOT SATISFIED:" in str(getattr(item, "text", "")) for item in content)


def _call_boundary(engine: Any, method: str, args: tuple[Any, ...], kwargs: dict[str, Any], original: Any) -> Any:
    request = {"args": _safe_args(args), "kwargs": _safe_args(kwargs)}
    _event("engine-call", method=method, phase="before", request=request, probe=_probe_context())
    if method == "pointer_drag":
        _capture_state("pointer-drag-before", engine, args=request)
    try:
        result = original(*args, **kwargs)
    except BaseException as error:
        _event("engine-call", method=method, phase="exception", request=request,
               error={"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
        _capture_state(f"{method}-exception", engine, args=request)
        raise
    _event("engine-call", method=method, phase="after", request=request, result=_json(result), probe=_probe_context())
    if method in {"session_connect", "app_launch"}:
        _capture_state(f"{method}-after", engine, args=request)
    elif method == "element_query":
        wait = getattr(result, "wait", None)
        if wait is not None and not getattr(wait, "satisfied", True):
            _capture_state("element-query-unsatisfied", engine, args=request)
    elif method == "pointer_drag":
        _capture_state("pointer-drag-after", engine, args=request)
    return result


def _patch() -> None:
    global _PATCHED, _CLI_PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    try:
        from kwin_mcp.core import AutomationEngine

        for name in ("session_connect", "app_launch", "element_query", "screen_capture", "pointer_drag"):
            original = getattr(AutomationEngine, name)

            def make_wrapper(method: str, function: Any):
                def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                    if self not in _ENGINES:
                        _ENGINES.append(self)
                    if method == "screen_capture" and _CAPTURING:
                        return function(self, *args, **kwargs)
                    return _call_boundary(self, method, args, kwargs, lambda *a, **k: function(self, *a, **k))

                return wrapper

            setattr(AutomationEngine, name, make_wrapper(name, original))
    except Exception as error:
        _event("patch-error", component="AutomationEngine", error={"type": type(error).__name__, "message": str(error)})

    try:
        import mcp_harness

        for cls_name in ("McpTestClient", "ModernMcpTestClient"):
            cls = getattr(mcp_harness, cls_name, None)
            if cls is None:
                continue
            for name in ("call_result", "call_structured", "call_model", "call_text", "call_tool_error"):
                original = getattr(cls, name)

                async def wrapper(self: Any, *args: Any, __name: str = name, __original: Any = original, **kwargs: Any) -> Any:
                    request = {"args": _safe_args(args), "kwargs": _safe_args(kwargs)}
                    _event("mcp-call", method=__name, phase="before", request=request)
                    try:
                        result = await __original(self, *args, **kwargs)
                    except BaseException as error:
                        _event("mcp-call", method=__name, phase="exception", request=request,
                               error={"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
                        for engine in list(_ENGINES):
                            _capture_state(f"mcp-{__name}-exception", engine, args=request)
                        if not _ENGINES:
                            _capture_state(f"mcp-{__name}-exception", None, args=request)
                        raise
                    _event("mcp-call", method=__name, phase="after", request=request, result=_json(result))
                    if getattr(result, "isError", False) or _mcp_unsatisfied(result):
                        for engine in list(_ENGINES):
                            _capture_state(f"mcp-{__name}-error", engine, args=request)
                        if not _ENGINES:
                            _capture_state(f"mcp-{__name}-error", None, args=request)
                    return result
                setattr(cls, name, wrapper)
    except Exception as error:
        _event("patch-error", component="mcp_harness", error={"type": type(error).__name__, "message": str(error)})

    try:
        from test_cli_surface import _Cli

        original = _Cli.run

        def cli_wrapper(self: Any, command: str, *args: Any, **kwargs: Any) -> Any:
            request = {"command": command, "args": _safe_args(args), "kwargs": _safe_args(kwargs)}
            _event("cli-call", phase="before", request=request)
            for engine in list(_ENGINES):
                _capture_state("cli-before", engine, args=request)
            try:
                result = original(self, command, *args, **kwargs)
            except BaseException as error:
                _event("cli-call", phase="exception", request=request,
                       error={"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
                for engine in list(_ENGINES):
                    _capture_state("cli-exception", engine, args=request)
                raise
            _event("cli-call", phase="after", request=request, result=_json(result))
            for engine in list(_ENGINES):
                _capture_state("cli-after", engine, args=request)
            return result

        _Cli.run = cli_wrapper
        _CLI_PATCHED = True
    except Exception as error:
        _event("patch-error", component="test_cli_surface._Cli", error={"type": type(error).__name__, "message": str(error)})

def _patch_cli_if_loaded() -> None:
    global _CLI_PATCHED
    if _CLI_PATCHED:
        return
    module = next(
        (candidate for name, candidate in sys.modules.items()
         if name.rsplit(".", 1)[-1] == "test_cli_surface"),
        None,
    )
    cls = getattr(module, "_Cli", None) if module is not None else None
    if cls is None:
        return
    original = cls.run

    def cli_wrapper(self: Any, command: str, *args: Any, **kwargs: Any) -> Any:
        request = {"command": command, "args": _safe_args(args), "kwargs": _safe_args(kwargs)}
        _event("cli-call", phase="before", request=request)
        for engine in list(_ENGINES):
            _capture_state("cli-before", engine, args=request)
        try:
            result = original(self, command, *args, **kwargs)
        except BaseException as error:
            _event("cli-call", phase="exception", request=request,
                   error={"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
            for engine in list(_ENGINES):
                _capture_state("cli-exception", engine, args=request)
            raise
        _event("cli-call", phase="after", request=request, result=_json(result))
        for engine in list(_ENGINES):
            _capture_state("cli-after", engine, args=request)
        return result

    cls.run = cli_wrapper
    _CLI_PATCHED = True



def pytest_sessionstart(session: Any) -> None:
    _write("plugin-environment.json", {"environment": _selected_environment(), "pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid()})
    _patch()

def pytest_runtest_setup(item: Any) -> None:
    _patch_cli_if_loaded()



def pytest_runtest_logreport(report: Any) -> None:
    _event("pytest-report", nodeid=report.nodeid, when=report.when, outcome=report.outcome,
           longrepr=str(report.longrepr)[:20000] if report.failed else None)
    if report.failed:
        for index, engine in enumerate(list(_ENGINES)):
            _capture_state(f"failure-{report.when}-{index}", engine, args={"nodeid": report.nodeid, "when": report.when})


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    _event("pytest-session-finish", exitstatus=exitstatus)
