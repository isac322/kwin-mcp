"""Virtual session lifecycle — verified by observing the compositor itself."""

from __future__ import annotations

from tests.integration._helpers import call_text, mcp_stdio_session, running_kwin


async def test_session_start_brings_up_compositor() -> None:
    """``session_start`` produces a live Wayland compositor; ``session_stop`` kills it."""
    async with mcp_stdio_session() as client:
        start = await call_text(
            client,
            "session_start",
            {"enable_clipboard": False, "keep_screenshots": False},
        )
        assert "Session started" in start
        assert "Wayland socket: wayland-mcp-" in start

        info = await call_text(client, "wayland_info", {})
        assert "wl_compositor" in info, f"Compositor not reachable after start: {info}"

        stop = await call_text(client, "session_stop", {})
        assert stop == "Session stopped."


async def test_wayland_info_lists_expected_protocols() -> None:
    """Virtual compositor exposes both core and xdg_shell protocols."""
    async with running_kwin() as client:
        info = await call_text(client, "wayland_info", {})
    assert "wl_compositor" in info
    assert "xdg_wm_base" in info, (
        f"xdg_wm_base missing from wayland-info — toolkit windows would fail:\n{info}"
    )
