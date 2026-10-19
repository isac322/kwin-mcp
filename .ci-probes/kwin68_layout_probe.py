"""Temporary native-only observations; no synthetic pixels or topology.

QA: require initial rendered ScreenShot2 PNG and proven mapping first, observe
QA-CAP-007's exact mixed-scale configuration, then a real 200px position gap,
then QA-CAP-008's region burst across an actual post-capture output move.
A completed diagnostic is not a claim that all observed layouts are supported.
"""

import dataclasses
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback

from PIL import Image

# The helper is mounted outside the original image's unchanged test tree.
sys.path.insert(0, str(Path.cwd() / "tests"))
sys.path.insert(0, str(Path.cwd() / "tests" / "e2e"))
from session_harness import live_kwin
from test_screen_capture import kscreen_doctor, screen_box, wait_for_screens
from test_screen_capture_visual import kwin_screens

from kwin_mcp import screenshot
from kwin_mcp.core import AutomationEngine
from kwin_mcp.models import Capture, Locator, Rect, Wait

OUT = Path("/artifacts")


def error_record(error):
    return {"type": type(error).__name__, "message": str(error),
            "traceback": traceback.format_exc()}


def files(directory):
    return {str(path.relative_to(directory)): {
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    } for path in sorted(directory.rglob("*")) if path.is_file()}


def retain_png(frame, label, screens):
    """Copy published native bytes verbatim; inspect without conversion/masking."""
    source = Path(frame.path)
    target = OUT / f"{label}.png"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    shutil.copyfile(source, target)
    result = {"source": str(source), "copy": str(target),
              "source_sha256": source_digest,
              "copy_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
              "bytes": source.stat().st_size,
              "frame": frame.model_dump(mode="json")}
    with Image.open(source) as image:
        image.load()
        result.update(format=image.format, mode=image.mode, size=list(image.size),
                      bands=list(image.getbands()))
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        result["alpha_range"] = list(alpha.getextrema()) if alpha is not None else None
        boxes = sorted((screen_box(screen) for screen in screens), key=lambda box: box[0])
        if len(boxes) == 2:
            left, right = boxes
            gap = (left[2], max(left[1], right[1]), right[0], min(left[3], right[3]))
            if gap[0] < gap[2] and gap[1] < gap[3]:
                center = ((gap[0] + gap[2]) // 2, (gap[1] + gap[3]) // 2)
                observed = {"logical_box": list(gap), "logical_center": list(center)}
                if frame.mapping is None:
                    observed["unavailable"] = "Frame has no proven mapping; no pixel guessed"
                else:
                    mapping = frame.mapping
                    pixel = (round((center[0] - mapping.origin.x + 0.5) * mapping.scale - 0.5),
                             round((center[1] - mapping.origin.y + 0.5) * mapping.scale - 0.5))
                    observed["pixel_center"] = list(pixel)
                    if 0 <= pixel[0] < image.width and 0 <= pixel[1] < image.height:
                        observed["native_pixel"] = image.getpixel(pixel)
                        observed["alpha"] = alpha.getpixel(pixel) if alpha is not None else None
                        if alpha is None:
                            observed["alpha_unavailable"] = f"Native PNG mode is {image.mode}; no alpha channel"
                    else:
                        observed["unavailable"] = "Mapped gap center lies outside the native PNG"
                result["gap"] = observed
    return result


def test_native_layouts(tmp_path, monkeypatch):
    observations = {"diagnostic_only": True, "initial_render_proven": False,
                    "test_outcome_does_not_assert_all_layouts_supported": True,
                    "stages": [], "qa8": {"attempted": False}}
    original_query = screenshot.query_topology
    engine = AutomationEngine()

    def save():
        (OUT / "layout-observations.json").write_text(json.dumps(observations, indent=2) + "\n")

    def record_resource(kind, identifier, delete_command):
        with (OUT / "layout-resources.jsonl").open("a") as ledger:
            ledger.write(json.dumps({"kind": kind, "identifier": identifier,
                                     "delete_command": delete_command,
                                     "created_at": time.time()}) + "\n")

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("KWIN_MCP_X11_SCREENSHOT", raising=False)
    save()
    try:
        with live_kwin(output_count=2) as live:
            record_resource("process-group", live.process.pid, f"kill -TERM -- -{live.process.pid}")
            record_resource("socket", str(live.socket_path), f"rm -- {live.socket_path}")
            screens = wait_for_screens(live.dbus_address, lambda value: len(value) == 2)
            initial_screens = sorted(screens, key=lambda screen: screen["x"])
            first, second = initial_screens
            info = engine.session_connect(dbus_address=live.dbus_address,
                                          wayland_display=live.wayland_display)
            assert info.kind == "live", info.render()
            shots = Path(info.artifacts_dir)
            record_resource("scratch-directory", str(shots), f"rm -rf -- {shots}")
            record_resource("scratch-directory", str(tmp_path), f"rm -rf -- {tmp_path}")

            def configure(*arguments):
                kscreen_doctor(*arguments, dbus_address=live.dbus_address,
                               wayland_display=live.wayland_display, home=tmp_path / "kscreen-home")

            def capture(label, required=False):
                stage = {"name": label, "files_before": files(shots)}
                observations["stages"].append(stage)
                try:
                    stage["screens_before"] = kwin_screens(live.dbus_address)
                    stage["topology_before"] = dataclasses.asdict(original_query(live.dbus_address))
                    frame = engine.screen_capture(max_edge=0)
                    stage["capture"] = retain_png(frame, label, stage["screens_before"])
                    stage["topology_after"] = dataclasses.asdict(original_query(live.dbus_address))
                    stage["outcome"] = "captured"
                    if required:
                        proof = stage["capture"]
                        boxes = [screen_box(screen) for screen in stage["screens_before"]]
                        origin = (min(box[0] for box in boxes), min(box[1] for box in boxes))
                        union = (max(box[2] for box in boxes) - origin[0],
                                 max(box[3] for box in boxes) - origin[1])
                        assert proof["format"] == "PNG" and proof["bytes"] > 10_000, proof
                        assert proof["source_sha256"] == proof["copy_sha256"], proof
                        assert (frame.width, frame.height) == union, frame
                        assert frame.coverage == "full" and frame.mapping is not None, frame
                        assert frame.mapping.scale == 1, frame
                        assert (frame.mapping.origin.x, frame.mapping.origin.y) == origin, frame
                        assert "backend screenshot2" in frame.coordinate_space, frame
                        assert stage["topology_before"] == stage["topology_after"], stage
                        observations["initial_render_proven"] = True
                except Exception as error:
                    stage.update(outcome="exception", exception=error_record(error))
                    if required:
                        raise
                finally:
                    stage["files_after"] = files(shots)
                    try:
                        stage["screens_after"] = kwin_screens(live.dbus_address)
                        if "topology_after" not in stage:
                            stage["topology_after"] = dataclasses.asdict(original_query(live.dbus_address))
                    except Exception as error:
                        stage["final_observation_exception"] = error_record(error)
                    save()

            try:
                launched = engine.app_launch("kcalc")
                observations["launched_app"] = launched.model_dump(mode="json")
                assert launched.pid > 0, launched
                found = engine.element_query(Locator(app="kcalc"), wait=Wait(timeout_ms=15_000))
                assert found.wait is not None and found.wait.satisfied, found.render()
                capture("initial", required=True)

                # Identical scale and positions to QA-CAP-007; no error oracle changed.
                mixed = {"name": "mixed-configuration", "scale": 1.45}
                observations["stages"].append(mixed)
                try:
                    configure(f"output.{second['name']}.scale.1.45",
                              f"output.{second['name']}.position.0,0",
                              f"output.{first['name']}.position.{round(1280 / 1.45)},0")
                    mixed["screens"] = wait_for_screens(live.dbus_address, lambda value: any(
                        screen["name"] == second["name"] and abs(screen["scale"] - 1.45) < 1e-3
                        for screen in value))
                    mixed["outcome"] = "configured"
                    capture("mixed-scale")
                except Exception as error:
                    mixed.update(outcome="exception", exception=error_record(error))
                finally:
                    save()

                gap = {"name": "gap-configuration", "requested_gap_px": 200}
                observations["stages"].append(gap)
                try:
                    edge = round(first["width"])
                    configure(f"output.{first['name']}.scale.1",
                              f"output.{second['name']}.scale.1",
                              f"output.{first['name']}.position.0,0",
                              f"output.{second['name']}.position.{edge + 200},0")
                    gap["screens"] = wait_for_screens(live.dbus_address, lambda value: (
                        len(value) == 2 and all(abs(screen["scale"] - 1) < 1e-3 for screen in value)
                        and next(screen for screen in value if screen["name"] == second["name"])["x"]
                        - sum(next(screen for screen in value if screen["name"] == first["name"])[key]
                              for key in ("x", "width")) == 200))
                    gap["outcome"] = "configured"
                    capture("position-gap")
                except Exception as error:
                    gap.update(outcome="exception", exception=error_record(error))
                finally:
                    save()

                qa8 = observations["qa8"]
                qa8.update(attempted=True, hook_calls=[], frames=[], warnings=[],
                           files_before=files(shots),
                           distinction="one-frame region burst, not one-shot retry or forced fallback")
                try:
                    # Establish an actual stable baseline independently of gap support.
                    configure(f"output.{first['name']}.scale.1", f"output.{second['name']}.scale.1",
                              f"output.{first['name']}.position.0,0",
                              f"output.{second['name']}.position.{round(first['width'])},0")
                    before = wait_for_screens(live.dbus_address, lambda value: all(
                        abs(screen["scale"] - 1) < 1e-3 for screen in value) and any(
                        screen["name"] == second["name"] and round(screen["x"]) == round(first["width"])
                        for screen in value))
                    qa8["screens_before"] = before
                    qa8["topology_before"] = dataclasses.asdict(original_query(live.dbus_address))

                    def move_before_post_query(address):
                        caller = sys._getframe(1).f_back
                        burst = caller if caller is not None and caller.f_code.co_name == "_burst_frame" else None
                        event = {"call": len(qa8["hook_calls"]) + 1,
                                 "burst_backend": burst.f_locals.get("backend") if burst else None,
                                 "phase": ("post" if "result" in burst.f_locals else "pre") if burst else "other"}
                        qa8["hook_calls"].append(event)
                        try:
                            if event["burst_backend"] == "screenshot2" and event["phase"] == "post" and not qa8.get("moved"):
                                event["operation"] = "move real output immediately before original post query"
                                new_x = round(first["width"]) + 200
                                configure(f"output.{second['name']}.position.{new_x},0")
                                event["screens_after_move"] = wait_for_screens(address, lambda value: any(
                                    screen["name"] == second["name"] and round(screen["x"]) == new_x
                                    for screen in value))
                                qa8["moved"] = True
                            topology = original_query(address)
                            event["returned_original_topology"] = dataclasses.asdict(topology)
                            return topology
                        except Exception as error:
                            event["exception"] = error_record(error)
                            raise
                        finally:
                            save()

                    with monkeypatch.context() as hook:
                        hook.setattr(screenshot, "query_topology", move_before_post_query)
                        pressed = engine.keyboard_press("shift", capture=Capture(
                            delays_ms=[0], region=Rect(x=0, y=0, width=200, height=100), max_edge=0))
                    qa8.update(frames=[frame.model_dump(mode="json") for frame in pressed.frames],
                               warnings=list(pressed.warnings), result=pressed.model_dump(mode="json"),
                               outcome="returned")
                    qa8["pngs"] = [retain_png(frame, f"qa8-{index}", before)
                                   for index, frame in enumerate(pressed.frames)]
                except Exception as error:
                    qa8.update(outcome="exception", exception=error_record(error))
                finally:
                    qa8["files_after"] = files(shots)
                    qa8["files_added"] = sorted(set(qa8["files_after"]) - set(qa8["files_before"]))
                    qa8["files_removed"] = sorted(set(qa8["files_before"]) - set(qa8["files_after"]))
                    qa8["files_changed"] = sorted(name for name in set(qa8["files_before"]) & set(qa8["files_after"])
                                                   if qa8["files_before"][name] != qa8["files_after"][name])
                    try:
                        qa8["screens_after"] = kwin_screens(live.dbus_address)
                        qa8["topology_after"] = dataclasses.asdict(original_query(live.dbus_address))
                    except Exception as error:
                        qa8["final_observation_exception"] = error_record(error)
                    save()
            finally:
                engine.session_stop()
    except Exception as error:
        observations["probe_exception"] = error_record(error)
        raise
    finally:
        save()
