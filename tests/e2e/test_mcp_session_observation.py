"""Installed-package MCP stdio coverage for the session and observation tools.

One virtual session is driven through the S call sequence (plan row 26):
``session_start``, ``app_launch``, ``app_log``, ``element_query``, ``window_list``,
``wayland_list``, ``dbus_call``, ``window_focus`` and ``window_close``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp_harness import running_mcp_server
from visual_harness import screenshot_provider_unavailable

if TYPE_CHECKING:
    from mcp_harness import McpTestClient

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
ENV_NAME = "KWIN_MCP_E2E_ENV"
ENV_VALUE = "installed-mcp-session-env"
ENV_COMMAND = f'sh -c \'printf "%s" "${ENV_NAME}"\''
APP_TIMEOUT_MS = 15_000
# A15: the timed-out wait must last at least its stated value -10 % and at most 3x.
WAIT_TIMEOUT_MS = 1_000
MISSING_ELEMENT = "definitely-not-a-real-kcalc-element"
LIST_NAMES = {
    "service": "org.freedesktop.DBus",
    "path": "/",
    "interface": "org.freedesktop.DBus",
    "method": "ListNames",
}


@pytest.fixture
def anyio_backend() -> str:
    """The MCP stdio client is exercised on asyncio."""
    return "asyncio"


def _windows_of(windows: list[dict[str, Any]], app: str) -> list[dict[str, Any]]:
    return [window for window in windows if app in window["app_id"].lower()]


async def _wait_for_window(client: McpTestClient, app: str) -> dict[str, Any]:
    """Poll window_list until exactly one window of ``app`` is listed."""
    deadline = anyio.current_time() + APP_TIMEOUT_MS / 1000
    text = ""
    while anyio.current_time() < deadline:
        structured, text = await client.call_structured("window_list")
        matches = _windows_of(structured["windows"], app)
        if len(matches) == 1:
            return matches[0]
        await anyio.sleep(0.25)
    pytest.fail(f"{app} never appeared once in window_list:\n{text[:1000]}")


@pytest.mark.anyio
async def test_installed_server_observes_virtual_session_and_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise session, process, AT-SPI, window, D-Bus and Wayland tools over stdio."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    async with running_mcp_server() as client:
        session_running = False
        try:
            connect_error = await client.call_tool_error("session_connect", lines=None)
            assert (
                "No D-Bus address available. Provide dbus_address parameter "
                "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
            ) in "\n".join(connect_error), connect_error
            assert await client.session.send_ping() is not None

            started, start_text = await client.call_structured(
                "session_start", {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
            )
            session_running = True
            assert started["kind"] == "virtual", started
            assert started["screen"] == {"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT}
            assert start_text.startswith("Session started. Wayland socket: wayland-mcp-"), (
                start_text
            )

            launched, launch_text = await client.call_structured(
                "app_launch", {"command": ENV_COMMAND, "env": {ENV_NAME: ENV_VALUE}}
            )
            assert launched["command"] == ENV_COMMAND, launched
            assert f"App launched: {ENV_COMMAND} (PID={launched['pid']})" in launch_text
            env_pid = launched["pid"]

            log_deadline = anyio.current_time() + 5
            env_log: dict[str, Any] = {}
            while anyio.current_time() < log_deadline:
                env_log, log_text = await client.call_structured(
                    "app_log", {"pid": env_pid, "last_n_lines": 10}
                )
                if env_log["lines"] == [ENV_VALUE]:
                    assert log_text.endswith(f"\n{ENV_VALUE}"), log_text
                    break
                await anyio.sleep(0.1)
            assert env_log.get("lines") == [ENV_VALUE], env_log

            bus_names, _ = await client.call_structured("dbus_call", LIST_NAMES)
            assert "org.kde.KWin" in bus_names["reply"], bus_names
            kwin_owner, owner_text = await client.call_structured(
                "dbus_call",
                {
                    **LIST_NAMES,
                    "method": "GetNameOwner",
                    "args": ["string:org.kde.KWin"],
                },
            )
            assert isinstance(kwin_owner["reply"], str), kwin_owner
            assert re.fullmatch(r":\d+\.\d+", kwin_owner["reply"]), kwin_owner
            assert owner_text.startswith(kwin_owner["reply"]), owner_text

            kcalc, kcalc_text = await client.call_structured("app_launch", {"command": "kcalc"})
            assert kcalc["command"] == "kcalc", kcalc
            assert kcalc_text.startswith("App launched: kcalc"), kcalc_text
            waited, waited_text = await client.call_structured(
                "element_query",
                {
                    "locator": {"app": "kcalc", "query": "Equals"},
                    "wait": {"timeout_ms": APP_TIMEOUT_MS},
                },
            )
            assert waited["wait"]["satisfied"] is True, waited_text[:500]
            assert any(e["name"] == "Equals" for e in waited["elements"]), waited_text[:500]
            assert '[button] "Equals"' in waited_text, waited_text[:500]

            buttons, buttons_text = await client.call_structured(
                "element_query", {"locator": {"app": "kcalc", "role": "button"}, "max_depth": 15}
            )
            roles = {element["role"] for element in buttons["elements"]}
            assert roles == {"button"}, buttons_text[:500]
            assert '[button] "Equals"' in buttons_text, buttons_text[:500]

            equals, equals_text = await client.call_structured(
                "element_query", {"locator": {"app": "kcalc", "query": "Equals"}}
            )
            assert equals["wait"] is None, equals
            assert equals["elements"], equals_text[:500]
            assert equals_text.startswith(
                f"Found {len(equals['elements'])} elements matching query='Equals', app='kcalc':"
            ), equals_text[:500]
            assert any(
                re.match(r'^\s*- \[[^]]+] "Equals"(?: |$)', line)
                for line in equals_text.splitlines()
            ), equals_text[:500]

            timeout_started = anyio.current_time()
            timed_out, timed_out_text = await client.call_structured(
                "element_query",
                {
                    "locator": {"app": "kcalc", "query": MISSING_ELEMENT},
                    "wait": {"timeout_ms": WAIT_TIMEOUT_MS, "poll_interval_ms": 50},
                },
            )
            timeout_elapsed = anyio.current_time() - timeout_started
            assert timed_out["elements"] == [], timed_out
            assert timed_out["wait"]["satisfied"] is False, timed_out
            assert timed_out["wait"]["timeout_ms"] == WAIT_TIMEOUT_MS, timed_out
            assert timed_out["wait"]["elapsed_ms"] >= WAIT_TIMEOUT_MS * 0.9, timed_out
            assert timed_out_text.split("\n") == [
                f"NOT SATISFIED: Timeout after {WAIT_TIMEOUT_MS} ms "
                f"(until=present, elapsed {timed_out['wait']['elapsed_ms']} ms)",
                f"No elements found matching query='{MISSING_ELEMENT}', app='kcalc'",
            ], timed_out_text
            assert timeout_elapsed >= WAIT_TIMEOUT_MS / 1000 * 0.9, timeout_elapsed
            assert timeout_elapsed <= WAIT_TIMEOUT_MS / 1000 * 3, timeout_elapsed

            _kwrite, kwrite_text = await client.call_structured("app_launch", {"command": "kwrite"})
            assert kwrite_text.startswith("App launched: kwrite"), kwrite_text
            kwrite_elements, kwrite_elements_text = await client.call_structured(
                "element_query",
                {"locator": {"app": "kwrite"}, "wait": {"timeout_ms": APP_TIMEOUT_MS}},
            )
            assert kwrite_elements["wait"]["satisfied"] is True, kwrite_elements_text[:500]
            assert kwrite_elements_text.startswith("Found "), kwrite_elements_text[:500]

            kcalc_window = await _wait_for_window(client, "kcalc")
            kwrite_window = await _wait_for_window(client, "kwrite")

            focused, focus_text = await client.call_structured(
                "window_focus", {"window": {"id": kcalc_window["id"]}}
            )
            assert focused["window_id"] == kcalc_window["id"], focused
            assert focus_text.startswith("Focused:"), focus_text
            focus_deadline = anyio.current_time() + 5
            active_text = ""
            while anyio.current_time() < focus_deadline:
                active, active_text = await client.call_structured(
                    "window_list", {"filter": {"active": True}}
                )
                if [w["id"] for w in active["windows"]] == [kcalc_window["id"]]:
                    break
                await anyio.sleep(0.25)
            else:
                pytest.fail(f"kcalc never became active: {active_text[:500]}")
            assert active_text.startswith("Active window:\n"), active_text
            assert "kcalc" in active_text.lower(), active_text
            assert "[active]" in active_text, active_text

            client_rect = active["windows"][0]["client"]
            assert 0 <= client_rect["x"] < SCREEN_WIDTH, client_rect
            assert 0 <= client_rect["y"] < SCREEN_HEIGHT, client_rect
            assert client_rect["x"] + client_rect["width"] <= SCREEN_WIDTH, client_rect
            assert client_rect["y"] + client_rect["height"] <= SCREEN_HEIGHT, client_rect

            closed, close_text = await client.call_structured(
                "window_close", {"window": {"id": kwrite_window["id"]}}
            )
            assert closed["requested"] is True, closed
            assert closed["consent"] == "not_required", closed
            assert close_text.startswith("Close requested:"), close_text
            close_deadline = anyio.current_time() + 10
            remaining_text = ""
            while anyio.current_time() < close_deadline:
                remaining, remaining_text = await client.call_structured(
                    "window_list", {"filter": {"id": kwrite_window["id"]}}
                )
                if remaining["windows"] == []:
                    assert remaining_text == f"No window with id {kwrite_window['id']!r}."
                    break
                await anyio.sleep(0.25)
            else:
                pytest.fail(f"kwrite window was not closed: {remaining_text[:500]}")
            still, _ = await client.call_structured(
                "window_list", {"filter": {"id": kcalc_window["id"]}}
            )
            assert len(still["windows"]) == 1, still

            seat, seat_text = await client.call_structured("wayland_list", {"filter": "wl_seat"})
            assert "interface: 'wl_seat'" in seat_text, seat_text[:500]
            assert "interface: 'wl_compositor'" not in seat_text, seat_text[:500]
            assert seat["lines"] and all("wl_seat" in line for line in seat["lines"]), seat
            assert any(g["interface"] == "wl_seat" for g in seat["globals"]), seat

            stopped, stop_text = await client.call_structured("session_stop")
            session_running = False
            assert stopped["stopped"] is True, stopped
            assert stop_text == "Session stopped.", stop_text
        finally:
            if session_running:
                await client.call_result("session_stop")


@pytest.mark.anyio
async def test_virtual_screen_capture_error_crosses_stdio_without_killing_server() -> None:
    """A real screenshot-provider outage stays an MCP tool error without ending the session."""
    async with running_mcp_server() as client:
        session_running = False
        try:
            started, start_text = await client.call_structured(
                "session_start", {"screen_width": SCREEN_WIDTH, "screen_height": SCREEN_HEIGHT}
            )
            session_running = True
            assert start_text.startswith("Session started."), start_text

            with screenshot_provider_unavailable(started["dbus_address"]):
                capture_error = "\n".join(
                    await client.call_tool_error("screen_capture", lines=None)
                )
                assert "Screenshot capture failed" in capture_error, capture_error
                assert "ScreenShot2" in capture_error, capture_error
                assert "Spectacle" in capture_error, capture_error

                bus_names, _ = await client.call_structured("dbus_call", LIST_NAMES)
                assert "org.kde.KWin" in bus_names["reply"], bus_names

            stopped, stop_text = await client.call_structured("session_stop")
            session_running = False
            assert stopped["stopped"] is True, stopped
            assert stop_text == "Session stopped.", stop_text
        finally:
            if session_running:
                await client.call_result("session_stop")
