"""Clipboard roundtrip — ``clipboard_set`` followed by ``clipboard_get``."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from tests.integration._helpers import call_text, running_kwin

pytestmark = pytest.mark.skipif(
    shutil.which("wl-copy") is None or shutil.which("wl-paste") is None,
    reason="wl-clipboard not installed",
)


async def test_clipboard_set_then_get_roundtrips() -> None:
    """Values written via ``clipboard_set`` are visible through ``clipboard_get``."""
    payload = "clipboard-roundtrip-probe"

    async with running_kwin() as client:
        await call_text(client, "clipboard_set", {"text": payload})
        # wl-copy is a background daemon — give it a moment to publish.
        await asyncio.sleep(0.3)

        got = await call_text(client, "clipboard_get", {})

    assert got == payload, f"Clipboard roundtrip mismatch: {got!r} != {payload!r}"
