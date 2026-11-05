"""End-to-end coverage of the virtual session's isolation from the host's runtime dir and data.

A virtual session shares ``$XDG_RUNTIME_DIR`` and ``~/.local/share`` with the desktop it runs on.
A stand-in for that desktop (a test-owned session bus with a document portal, a permission store
and, when installed, gvfs) checks that a session leaves it intact while launched apps keep the
host's runtime dir. ``KWIN_MCP_E2E_HOST_BUS`` points the tests at a real desktop session's bus
instead (a VM running GNOME or XFCE). Services the image lacks, and FUSE mounts a container
cannot make, skip with registered reasons.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _suite_policy import (
    HOST_FUSE_UNAVAILABLE_SKIP_PREFIX,
    HOST_SERVICE_MISSING_SKIP_PREFIX,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import AppLaunchResult, ElementList, SessionInfo

DOCS = (
    "org.freedesktop.portal.Documents",
    "/org/freedesktop/portal/documents",
    "org.freedesktop.portal.Documents",
)
PERMS = (
    "org.freedesktop.impl.portal.PermissionStore",
    "/org/freedesktop/impl/portal/PermissionStore",
    "org.freedesktop.impl.portal.PermissionStore",
)
GVFS = ("org.gtk.vfs.Daemon", "/org/gtk/vfs/Daemon", "org.freedesktop.DBus.Peer")
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def _wait_until(condition: Callable[[], bool], message: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, message
        time.sleep(0.5)


def _mounts() -> list[tuple[str, str]]:
    """(mount point, filesystem type) in mount order; not every image has findmnt."""
    lines = Path("/proc/self/mountinfo").read_text().splitlines()
    return [(f.split()[4], t.split()[0]) for f, _, t in (line.partition(" - ") for line in lines)]


def _fstype(path: Path) -> str:
    """The type of the topmost mount on ``path``, or "" when nothing is mounted there."""
    return next((t for m, t in reversed(_mounts()) if m == str(path)), "")


class HostDesktop:
    """The desktop a session runs on: a test-owned session bus, or a real one."""

    def __init__(self) -> None:
        self.daemon: subprocess.Popen[str] | None = None
        self.address = os.environ.get("KWIN_MCP_E2E_HOST_BUS", "")
        if not self.address:
            # The stand-in's services would take over a real desktop's (#132 itself)
            # and close() unmounts its doc and gvfs: never run it next to one.
            if (RUNTIME / "bus").exists() or any(_fstype(RUNTIME / m) for m in ("doc", "gvfs")):
                pytest.skip(
                    f"a desktop session runs in {RUNTIME}: use the E2E container "
                    "or set KWIN_MCP_E2E_HOST_BUS"
                )
            self.daemon = subprocess.Popen(
                ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert self.daemon.stdout is not None
            self.address = self.daemon.stdout.readline().strip()

    def try_call(
        self, service: tuple[str, str, str], method: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        name, path, interface = service
        cmd = ["gdbus", "call", "--address", self.address, "-d", name, "-o", path]
        return subprocess.run(
            [*cmd, "-m", f"{interface}.{method}", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def call(self, service: tuple[str, str, str], method: str, *args: str) -> str:
        out = self.try_call(service, method, *args)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def activatable(self, name: str) -> bool:
        bus = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
        return f"'{name}'" in self.call(bus, "ListActivatableNames")

    def owner_pid(self, name: str) -> int | None:
        out = subprocess.run(
            ["busctl", f"--address={self.address}", "status", name],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"^PID=(\d+)$", out.stdout, re.MULTILINE)
        return int(match.group(1)) if match else None

    def require(self, name: str) -> None:
        if not self.activatable(name):
            pytest.skip(f"{HOST_SERVICE_MISSING_SKIP_PREFIX}: {name}")

    def close(self) -> None:
        if self.daemon is None:
            return
        self.daemon.terminate()
        self.daemon.wait(timeout=10)
        for mount in ("doc", "gvfs"):
            subprocess.run(
                ["fusermount3", "-u", str(RUNTIME / mount)], capture_output=True, check=False
            )


@pytest.fixture
def host() -> Iterator[HostDesktop]:
    desktop = HostDesktop()
    try:
        yield desktop
    finally:
        desktop.close()


def test_host_document_portal_survives_session(
    host: HostDesktop, start_session: Callable[..., SessionInfo], engine: AutomationEngine
) -> None:
    host.require(DOCS[0])
    # Without FUSE the portal exits at startup ("fuse init failed"), so the call fails.
    if (
        host.try_call(DOCS, "GetMountPoint").returncode != 0
        or _fstype(RUNTIME / "doc") != "fuse.portal"
    ):
        pytest.skip(f"{HOST_FUSE_UNAVAILABLE_SKIP_PREFIX}: the host document portal did not mount")
    portal = host.owner_pid(DOCS[0])
    assert portal is not None, "busctl did not report the host portal's PID"
    earlier = set(Path("/tmp").glob("kwin-mcp-svc-*"))

    start_session()
    engine.dbus_call(*DOCS, "GetMountPoint")  # what an app in the session asking the portal does
    assert host.owner_pid(DOCS[0]) == portal, "the host's document portal was replaced"
    engine.session_stop()

    assert _fstype(RUNTIME / "doc") == "fuse.portal", "the host's document portal mount is gone"
    assert host.owner_pid(DOCS[0]) == portal
    left = sorted(str(p) for p in set(Path("/tmp").glob("kwin-mcp-svc-*")) - earlier)
    assert not left, f"session_stop left its service dir (and the portal's mount): {left}"


def test_host_permission_store_untouched(
    host: HostDesktop, start_session: Callable[..., SessionInfo], engine: AutomationEngine
) -> None:
    host.require(PERMS[0])
    host.call(PERMS, "SetPermission", "kwin-mcp-e2e", "true", "host-1", "app.x", "['yes']")
    try:
        start_session()
        engine.dbus_call(
            *PERMS,
            "SetPermission",
            [
                "string:kwin-mcp-e2e",
                "boolean:true",
                "string:session-1",
                "string:app.x",
                "array:string:yes",
            ],
        )
        engine.session_stop()

        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        db = data / "flatpak/db/kwin-mcp-e2e"
        assert b"session-1" not in db.read_bytes(), "the session wrote into the host's db"
        assert "host-1" in host.call(PERMS, "List", "kwin-mcp-e2e")
    finally:
        # With KWIN_MCP_E2E_HOST_BUS this is a real desktop's permission db.
        for entry in ("host-1", "session-1"):
            host.try_call(PERMS, "Delete", "kwin-mcp-e2e", entry)


def test_host_gvfs_mount_not_covered(
    host: HostDesktop, start_session: Callable[..., SessionInfo], engine: AutomationEngine
) -> None:
    host.require(GVFS[0])

    def gvfs_mounts() -> list[str]:
        return [m for m, t in _mounts() if t == "fuse.gvfsd-fuse"]

    host.call(GVFS, "Ping")  # activates gvfsd, which starts gvfsd-fuse
    deadline = time.monotonic() + 30
    while gvfs_mounts().count(str(RUNTIME / "gvfs")) != 1:
        if time.monotonic() > deadline:
            pytest.skip(f"{HOST_FUSE_UNAVAILABLE_SKIP_PREFIX}: the host gvfs did not mount")
        time.sleep(0.5)
    start_session()
    engine.dbus_call(*GVFS, "Ping")
    # FUSE works here (the host mounted), so the session's gvfs must mount in its own dir.
    _wait_until(
        lambda: any(m.startswith("/tmp/kwin-mcp-svc-") for m in gvfs_mounts()),
        "the session's gvfsd-fuse did not mount in its service dir",
    )
    assert gvfs_mounts().count(str(RUNTIME / "gvfs")) == 1, "the session's gvfs covered the host's"


SECOND_SESSION = """
import sys
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import Locator, Wait
engine = AutomationEngine()
engine.session_start(screen_width=1280, screen_height=800)
engine.app_launch("kcalc")
found = engine.element_query(locator=Locator(app="kcalc"), wait=Wait(timeout_ms=30000))
assert found.wait is not None and found.wait.satisfied, "kcalc never reached the second a11y bus"
print("ready", flush=True)
sys.stdin.readline()
engine.session_stop()
"""


def test_concurrent_sessions_keep_accessibility(
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    wait_for_app: Callable[[str], ElementList],
) -> None:
    # A second session runs in its own process, like a second MCP server.
    start_session()
    launch("kcalc")
    wait_for_app("kcalc")
    second = subprocess.Popen(
        [sys.executable, "-c", SECOND_SESSION],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert second.stdin is not None
    assert second.stdout is not None
    try:
        assert second.stdout.readline().strip() == "ready"
        # Connections made before the second session keep working; an app
        # started now must still reach the first session's own a11y bus.
        launch("kwrite")
        wait_for_app("kwrite")
    finally:
        # A second session that died early must not hide the first failure.
        with contextlib.suppress(BrokenPipeError):
            second.stdin.write("\n")
            second.stdin.flush()
        try:
            second.wait(timeout=60)
        except subprocess.TimeoutExpired:
            second.kill()
            raise
    # The second session's teardown must not take the first one's bus with it.
    launch("konsole")
    wait_for_app("konsole")


def test_apps_keep_host_runtime_dir(
    start_session: Callable[..., SessionInfo],
    launch: Callable[..., AppLaunchResult],
    tmp_path: Path,
) -> None:
    out = tmp_path / "runtime"
    start_session()
    launch(f"sh -c 'echo $XDG_RUNTIME_DIR > {out}'")
    _wait_until(
        lambda: out.exists() and out.read_text().endswith("\n"),
        "the launched app did not report its runtime dir",
    )
    assert out.read_text().strip() == str(RUNTIME), "launched apps lost the host's runtime dir"


def test_session_gui_service_reaches_compositor(
    host: HostDesktop, start_session: Callable[..., SessionInfo], engine: AutomationEngine
) -> None:
    # Services the session bus activates are GUI processes: they need the session's display.
    host.require("org.freedesktop.impl.portal.desktop.gtk")
    start_session()
    reply = engine.dbus_call(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Settings",
        "ReadOne",
        ["string:org.freedesktop.appearance", "string:color-scheme"],
    )
    assert reply.signature.startswith("v"), reply.render()
