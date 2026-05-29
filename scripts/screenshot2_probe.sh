#!/usr/bin/env bash
set -euo pipefail

WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/screenshot2_probe_$$}"
SOCKET_PREFIX="wayland-spike-$$"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

PY_HELPER="$(mktemp /tmp/screenshot2_probe_helper.XXXXXX.py)"
INNER_SCRIPT="$(mktemp /tmp/screenshot2_probe_inner.XXXXXX.sh)"

mkdir -p "${OUTPUT_DIR}"

cleanup_kwin_processes() {
    pkill -f "kwin_wayland .*--socket ${SOCKET_PREFIX}" 2>/dev/null || true
    sleep 0.2
    if leftovers="$(pgrep -af "[k]win_wayland .*--socket ${SOCKET_PREFIX}" 2>/dev/null)"; then
        printf 'leftover_check=found\n%s\n' "${leftovers}" >&2
        return 1
    fi
    return 0
}

cleanup_all() {
    cleanup_kwin_processes >/dev/null 2>&1 || true
    rm -f "${RUNTIME_DIR}/${SOCKET_PREFIX}"* 2>/dev/null || true
    rm -f "${PY_HELPER}" "${INNER_SCRIPT}" 2>/dev/null || true
}
trap cleanup_all EXIT INT TERM HUP

cat >"${PY_HELPER}" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

import dbus
import dbus.bus
from PIL import Image

METHODS = ("CaptureActiveScreen", "CaptureWorkspace", "CaptureArea")


def _read_pipe(read_fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
    return b"".join(chunks)


def _clean_field(value: object) -> str:
    return str(value).replace("\n", " ").replace("|", "/")


def _dbus_error(exc: dbus.DBusException) -> str:
    return f"{exc.get_dbus_name()}: {exc.get_dbus_message()}"


def _emit(
    label: str,
    method: str,
    status: str,
    width: int = 0,
    height: int = 0,
    stride: int = 0,
    png_path: str = "",
    png_size: int = 0,
    error: object = "",
) -> None:
    print(
        "|".join(
            (
                _clean_field(label),
                _clean_field(method),
                _clean_field(status),
                str(width),
                str(height),
                str(stride),
                _clean_field(png_path),
                str(png_size),
                _clean_field(error),
            )
        ),
        flush=True,
    )


def _call_capture(iface: dbus.Interface, method: str, options: dbus.Dictionary) -> tuple[dict, bytes]:
    read_fd, write_fd = os.pipe()
    try:
        try:
            if method == "CaptureActiveScreen":
                results = iface.CaptureActiveScreen(options, dbus.types.UnixFd(write_fd))
            elif method == "CaptureWorkspace":
                results = iface.CaptureWorkspace(options, dbus.types.UnixFd(write_fd))
            elif method == "CaptureArea":
                results = iface.CaptureArea(
                    dbus.Int32(0),
                    dbus.Int32(0),
                    dbus.Int32(1),
                    dbus.Int32(1),
                    options,
                    dbus.types.UnixFd(write_fd),
                )
            else:
                raise ValueError(f"unknown method: {method}")
        finally:
            os.close(write_fd)
    except Exception:
        os.close(read_fd)
        raise
    return dict(results), _read_pipe(read_fd)


def _capture(label: str, method: str, output_dir: Path) -> bool:
    dbus_address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")
    options = dbus.Dictionary(
        {
            "include-cursor": dbus.Boolean(False),
            "include-decoration": dbus.Boolean(True),
        },
        signature="sv",
    )

    results, data = _call_capture(iface, method, options)
    width = int(results.get("width", 0))
    height = int(results.get("height", 0))
    stride = int(results.get("stride", 0))
    png_path = output_dir / f"{label}__{method}.png"

    if not data:
        _emit(label, method, "FAIL", width, height, stride, str(png_path), 0, "no data")
        return False
    if width <= 0 or height <= 0 or stride <= 0:
        _emit(label, method, "FAIL", width, height, stride, str(png_path), 0, "invalid geometry")
        return False

    image = Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride)
    image.save(png_path, "PNG")
    _emit(label, method, "OK", width, height, stride, str(png_path), png_path.stat().st_size, "")
    return True


def main() -> int:
    label = sys.argv[1]
    output_dir = Path(os.environ["OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)

    had_failure = False
    for method in METHODS:
        try:
            if not _capture(label, method, output_dir):
                had_failure = True
        except dbus.DBusException as exc:
            had_failure = True
            _emit(label, method, "FAIL", error=_dbus_error(exc))
        except Exception as exc:  # noqa: BLE001 - probe must report and continue.
            had_failure = True
            _emit(label, method, "FAIL", error=f"{type(exc).__name__}: {exc}")
    return 1 if had_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

cat >"${INNER_SCRIPT}" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

APP_PIDS=()

cleanup_session() {
    if ((${#APP_PIDS[@]})); then
        kill "${APP_PIDS[@]}" 2>/dev/null || true
        wait "${APP_PIDS[@]}" 2>/dev/null || true
    fi
    if [[ -n "${KWIN_PID:-}" ]]; then
        kill "${KWIN_PID}" 2>/dev/null || true
        wait "${KWIN_PID}" 2>/dev/null || true
    fi
    if [[ -n "${AT_SPI_PID:-}" ]]; then
        kill "${AT_SPI_PID}" 2>/dev/null || true
        wait "${AT_SPI_PID}" 2>/dev/null || true
    fi
    rm -f "${RUNTIME_DIR}/${SOCKET_NAME}" "${RUNTIME_DIR}/${SOCKET_NAME}.lock" 2>/dev/null || true
}
trap cleanup_session EXIT INT TERM HUP

LOG_LABEL="${LABEL//[^A-Za-z0-9_.-]/_}"
SCENARIO="${SCENARIO:-empty}"

echo "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS}"
echo "session_socket=${SOCKET_NAME}"
echo "scenario=${SCENARIO}"

/usr/lib/at-spi-bus-launcher --launch-immediately \
    >/tmp/screenshot2_probe_atspi_${LOG_LABEL}.log 2>&1 &
AT_SPI_PID=$!
sleep 0.2

dbus-update-activation-environment \
    WAYLAND_DISPLAY="${SOCKET_NAME}" \
    QT_QPA_PLATFORM=wayland \
    >/dev/null 2>&1 || true

env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    KDE_FULL_SESSION=true \
    KDE_SESSION_VERSION=6 \
    XDG_SESSION_TYPE=wayland \
    XDG_CURRENT_DESKTOP=KDE \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    QT_ACCESSIBILITY=1 \
    ATSPI_DBUS_IMPLEMENTATION=dbus-daemon \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    ${EXTRA_ENV_ASSIGNMENTS:-} \
    kwin_wayland --virtual --no-lockscreen \
    --width "${WIDTH}" --height "${HEIGHT}" \
    --socket "${SOCKET_NAME}" >/tmp/screenshot2_probe_kwin_${LOG_LABEL}.log 2>&1 &
KWIN_PID=$!

deadline=$((SECONDS + 10))
while [[ ! -e "${RUNTIME_DIR}/${SOCKET_NAME}" ]]; do
    if ! kill -0 "${KWIN_PID}" 2>/dev/null; then
        echo "kwin_start_failed: $(command tail -n 20 /tmp/screenshot2_probe_kwin_${LOG_LABEL}.log 2>/dev/null || true)"
        exit 0
    fi
    if ((SECONDS >= deadline)); then
        echo "kwin_start_failed: timeout waiting for ${RUNTIME_DIR}/${SOCKET_NAME}"
        exit 0
    fi
    sleep 0.1
done
sleep 0.5

case "${SCENARIO}" in
    empty)
        ;;
    kcalc)
        WAYLAND_DISPLAY="${SOCKET_NAME}" QT_QPA_PLATFORM=wayland kcalc \
            >/tmp/screenshot2_probe_kcalc_${LOG_LABEL}.log 2>&1 &
        APP_PIDS+=("$!")
        sleep 1.5
        ;;
    multi)
        WAYLAND_DISPLAY="${SOCKET_NAME}" QT_QPA_PLATFORM=wayland kcalc \
            >/tmp/screenshot2_probe_kcalc_${LOG_LABEL}.log 2>&1 &
        APP_PIDS+=("$!")
        WAYLAND_DISPLAY="${SOCKET_NAME}" QT_QPA_PLATFORM=wayland kdialog \
            --title "Dialog-One" --msgbox "Dialog-One" \
            >/tmp/screenshot2_probe_kdialog_one_${LOG_LABEL}.log 2>&1 &
        APP_PIDS+=("$!")
        WAYLAND_DISPLAY="${SOCKET_NAME}" QT_QPA_PLATFORM=wayland kdialog \
            --title "Dialog-Two" --msgbox "Dialog-Two" \
            >/tmp/screenshot2_probe_kdialog_two_${LOG_LABEL}.log 2>&1 &
        APP_PIDS+=("$!")
        sleep 2.0
        ;;
    *)
        echo "unknown_scenario=${SCENARIO}"
        exit 0
        ;;
esac

python3 "${PY_HELPER}" "${LABEL}" || true
SH
chmod +x "${INNER_SCRIPT}"

phase1_labels=(
    "baseline"
    "kwin_prefer_sw_qpainter"
    "libgl_always_software"
    "mesa_swrast"
    "egl_surfaceless"
    "libgl_mesa_swrast"
    "libgl_egl_surfaceless"
    "kwin_compose_o"
    "kwin_compose_q"
    "libgl_mesa_swrast_compose_o"
)

phase1_envs=(
    ""
    "KWIN_PREFER_SW_QPAINTER=1"
    "LIBGL_ALWAYS_SOFTWARE=1"
    "MESA_LOADER_DRIVER_OVERRIDE=swrast"
    "EGL_PLATFORM=surfaceless"
    "LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=swrast"
    "LIBGL_ALWAYS_SOFTWARE=1 EGL_PLATFORM=surfaceless"
    "KWIN_COMPOSE=O"
    "KWIN_COMPOSE=Q"
    "LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=swrast KWIN_COMPOSE=O"
)

phase2_scenarios=("empty" "kcalc" "multi")

run_probe() {
    local phase="$1"
    local label="$2"
    local scenario="$3"
    local extra_env="$4"
    local socket_name="$5"

    echo
    echo "## phase=${phase} label=${label} scenario=${scenario} extra_env=${extra_env:-<none>}"
    rm -f "${RUNTIME_DIR}/${socket_name}" "${RUNTIME_DIR}/${socket_name}.lock" 2>/dev/null || true
    LABEL="${label}" \
    SCENARIO="${scenario}" \
    WIDTH="${WIDTH}" \
    HEIGHT="${HEIGHT}" \
    SOCKET_NAME="${socket_name}" \
    SOCKET_PREFIX="${SOCKET_PREFIX}" \
    RUNTIME_DIR="${RUNTIME_DIR}" \
    PY_HELPER="${PY_HELPER}" \
    OUTPUT_DIR="${OUTPUT_DIR}" \
    EXTRA_ENV_ASSIGNMENTS="${extra_env}" \
    KDE_FULL_SESSION=true \
    KDE_SESSION_VERSION=6 \
    XDG_SESSION_TYPE=wayland \
    XDG_CURRENT_DESKTOP=KDE \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    QT_ACCESSIBILITY=1 \
    ATSPI_DBUS_IMPLEMENTATION=dbus-daemon \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    dbus-run-session bash "${INNER_SCRIPT}" || true
    rm -f "${RUNTIME_DIR}/${socket_name}" "${RUNTIME_DIR}/${socket_name}.lock" 2>/dev/null || true
}

echo "ScreenShot2 virtual-session feasibility probe"
echo "kwin_version=$(kwin_wayland --version 2>&1 || true)"
echo "session_model=fresh dbus-run-session + kwin_wayland --virtual per probe row"
echo "required_env=KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 ATSPI_DBUS_IMPLEMENTATION=dbus-daemon"
echo "geometry=${WIDTH}x${HEIGHT}"
echo "output_dir=${OUTPUT_DIR}"
echo "output_format=label|method|status|width|height|stride|png_path|png_size|error"

for index in "${!phase1_labels[@]}"; do
    run_probe \
        "1" \
        "${phase1_labels[$index]}" \
        "empty" \
        "${phase1_envs[$index]}" \
        "${SOCKET_PREFIX}-phase1-${index}"
done

for index in "${!phase2_scenarios[@]}"; do
    scenario="${phase2_scenarios[$index]}"
    run_probe \
        "2" \
        "phase2_${scenario}" \
        "${scenario}" \
        "" \
        "${SOCKET_PREFIX}-phase2-${index}"
done

echo
if cleanup_kwin_processes; then
    echo "leftover_check=clean"
else
    echo "leftover_check=found"
fi
rm -f "${RUNTIME_DIR}/${SOCKET_PREFIX}"* 2>/dev/null || true
echo "probe_complete output_dir=${OUTPUT_DIR}"
