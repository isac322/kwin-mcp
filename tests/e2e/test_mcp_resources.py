"""MCP resources over stdio: the kwin:// twins of tool results, pid completion, refusals.

``kwin://screenshots/{name}`` needs a real frame, so the twin test connects to
the visual fixture: native KWin 6.8+ uses ScreenShot2, while legacy KWin uses
the nested X11 capture backend.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import (
    BlobResourceContents,
    ResourceTemplateReference,
    TextContent,
    TextResourceContents,
)
from mcp_harness import running_mcp_server
from visual_harness import nested_visual_kwin
from window_zoo import APP_ID, ZooProcess

if TYPE_CHECKING:
    from mcp.types import CallToolResult
    from mcp_harness import McpTestClient

APP_LOG_TEMPLATE = "kwin://apps/{pid}/log"
STATIC_RESOURCES = {"kwin://session", "kwin://windows", "kwin://desktops"}
TEMPLATES = {APP_LOG_TEMPLATE, "kwin://screenshots/{name}"}
LOG_LINES = ["kwin-mcp-resource-log-1", "kwin-mcp-resource-log-2"]
LOG_COMMAND = f"sh -c 'echo {LOG_LINES[0]}; echo {LOG_LINES[1]}; exec sleep 120'"
REF_TEXT = re.compile(r"\be\d+\.\d+\b")
LOG_TIMEOUT_S = 15.0


@pytest.fixture
def anyio_backend() -> str:
    """The installed MCP client runs on asyncio."""
    return "asyncio"


def _text_body(result: Any, mime_type: str) -> str:
    (content,) = result.contents
    assert isinstance(content, TextResourceContents), content
    assert content.mime_type == mime_type, content.mime_type
    return content.text


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _assert_no_resource_link(result: CallToolResult) -> None:
    assert [content.type for content in result.content] == ["text"], result.content


async def _start_virtual(client: McpTestClient) -> None:
    info, _ = await client.call_structured(
        "session_start", {"screen_width": 1280, "screen_height": 800}
    )
    assert info["kind"] == "virtual", info


async def _launch(client: McpTestClient, command: str) -> dict[str, Any]:
    launched, _ = await client.call_structured("app_launch", {"command": command})
    assert launched["log_uri"] == f"kwin://apps/{launched['pid']}/log", launched
    return launched


async def _wait_for_log(client: McpTestClient, pid: int) -> dict[str, Any]:
    with anyio.fail_after(LOG_TIMEOUT_S):
        while True:
            log, _ = await client.call_structured("app_log", {"pid": pid, "last_n_lines": 0})
            if log["lines"][: len(LOG_LINES)] == LOG_LINES:
                return log
            await anyio.sleep(0.2)


@pytest.mark.anyio
@pytest.mark.visual
@pytest.mark.qa("QA-MCP-008")
async def test_resource_twins_equal_tool_output() -> None:
    """Each kwin:// resource returns its tool's data; no resource_link, no a11y resource."""
    with (
        nested_visual_kwin() as visual,
        ZooProcess(wayland_display=visual.wayland_display, dbus_address=visual.dbus_address) as zoo,
    ):
        zoo.expect("window: Zoo")
        async with running_mcp_server(env=visual.capture_env) as client:
            await client.call_structured(
                "session_connect",
                {"dbus_address": visual.dbus_address, "wayland_display": visual.wayland_display},
            )
            try:
                launched = await _launch(client, LOG_COMMAND)
                pid = launched["pid"]
                await _wait_for_log(client, pid)
                # Mint refs, so a leak into a resource body would be visible.
                elements, _ = await client.call_structured(
                    "element_query",
                    {
                        "locator": {"app": APP_ID, "role": "button"},
                        "wait": {"until": "present", "timeout_ms": 15_000},
                    },
                )
                assert elements["elements"], elements

                results: list[CallToolResult] = []
                bodies: list[str] = []

                info = await client.call_result("session_info")
                results.append(info)
                body = _text_body(await client.read_resource("kwin://session"), "application/json")
                bodies.append(body)
                assert json.loads(body) == info.structured_content

                windows = await client.call_result("window_list")
                results.append(windows)
                body = _text_body(await client.read_resource("kwin://windows"), "application/json")
                bodies.append(body)
                assert windows.structured_content is not None
                assert any(w["app_id"] == APP_ID for w in windows.structured_content["windows"])
                assert json.loads(body) == windows.structured_content

                desktops = await client.call_result("desktop_list")
                results.append(desktops)
                body = _text_body(await client.read_resource("kwin://desktops"), "application/json")
                bodies.append(body)
                assert json.loads(body) == desktops.structured_content

                log = await client.call_result("app_log", {"pid": pid, "last_n_lines": 0})
                results.append(log)
                body = _text_body(await client.read_resource(launched["log_uri"]), "text/plain")
                bodies.append(body)
                assert log.structured_content is not None
                assert body == "\n".join(log.structured_content["lines"])
                assert body.split("\n")[: len(LOG_LINES)] == LOG_LINES, body

                shot = await client.call_result("screen_capture")
                results.append(shot)
                frame = shot.structured_content
                assert frame is not None, shot.content
                assert frame["uri"] == f"kwin://screenshots/{Path(frame['path']).name}", frame
                (blob,) = (await client.read_resource(frame["uri"])).contents
                assert isinstance(blob, BlobResourceContents), blob
                assert blob.mime_type == "image/png", blob.mime_type
                assert (
                    base64.b64decode(blob.blob, validate=True) == Path(frame["path"]).read_bytes()
                )
                # The frame text names the same URI.
                first = shot.content[0]
                assert isinstance(first, TextContent), shot.content
                assert f"resource {frame['uri']}" in first.text, first.text

                for result in results:
                    assert not result.is_error, result.content
                    _assert_no_resource_link(result)

                for body in bodies:
                    assert "kwin://a11y" not in body, body
                    assert REF_TEXT.search(body) is None, body
                for body in bodies[:3]:
                    assert "ref" not in _keys(json.loads(body)), body

                listed = await client.session.list_resources()
                templates = await client.session.list_resource_templates()
                uris = {str(resource.uri) for resource in listed.resources}
                assert uris == STATIC_RESOURCES, uris
                patterns = {template.uri_template for template in templates.resource_templates}
                assert patterns == TEMPLATES, patterns
                assert not any("a11y" in uri for uri in uris | patterns), uris | patterns
            finally:
                await client.call_structured("session_stop")


@pytest.mark.anyio
@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.qa("QA-MCP-009")
async def test_resources_list_static() -> None:
    """resources/list and resources/templates/list never change; no listChanged."""
    async with running_mcp_server() as client:
        capability = client.initialize_result.capabilities.resources
        assert capability is not None
        assert not capability.list_changed, capability

        async def snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
            listed = await client.session.list_resources()
            templates = await client.session.list_resource_templates()
            return (
                listed.model_dump(mode="json", by_alias=True),
                templates.model_dump(mode="json", by_alias=True),
            )

        before = await snapshot()
        uris = {resource["uri"] for resource in before[0]["resources"]}
        assert uris == STATIC_RESOURCES, uris
        await _start_virtual(client)
        try:
            await _launch(client, "sleep 120")
            during = await snapshot()
        finally:
            await client.call_structured("session_stop")
        after = await snapshot()
        assert during == before
        assert after == before


@pytest.mark.anyio
@pytest.mark.qa("QA-MCP-010")
async def test_resource_path_traversal_refused() -> None:
    """Traversal and malformed names are resource errors that return no content."""
    async with running_mcp_server() as client:
        await _start_virtual(client)
        try:
            launched = await _launch(client, "sleep 120")
            # The valid log URI reads, so each refusal below is about the URI alone.
            body = _text_body(await client.read_resource(launched["log_uri"]), "text/plain")
            assert "root:" not in body
            for uri in (
                "kwin://screenshots/..%2F..%2Fetc%2Fpasswd",
                "kwin://screenshots/../x.png",
                "kwin://apps/../log",
                "kwin://apps/abc/log",
            ):
                with pytest.raises(MCPError) as caught:
                    await client.read_resource(uri)
                message = caught.value.error.message
                assert "root:" not in message, (uri, message)
        finally:
            await client.call_structured("session_stop")


@pytest.mark.anyio
@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.qa("QA-MCP-011")
async def test_completion_on_pid() -> None:
    """Completing pid of kwin://apps/{pid}/log lists the launched PIDs filtered by prefix."""
    ref = ResourceTemplateReference(type="ref/resource", uri=APP_LOG_TEMPLATE)
    async with running_mcp_server() as client:
        empty = await client.complete(ref, {"name": "pid", "value": ""})
        assert empty.completion.values == [], empty
        await _start_virtual(client)
        try:
            for _ in range(3):
                await _launch(client, "sleep 120")
            info, _ = await client.call_structured("session_info")
            pids = [str(app["pid"]) for app in info["apps"]]
            assert len(pids) == 3, info

            everything = await client.complete(ref, {"name": "pid", "value": ""})
            assert sorted(everything.completion.values) == sorted(pids), everything

            prefix = pids[0][0]
            filtered = await client.complete(ref, {"name": "pid", "value": prefix})
            assert sorted(filtered.completion.values) == sorted(
                pid for pid in pids if pid.startswith(prefix)
            ), filtered
            exact = await client.complete(ref, {"name": "pid", "value": pids[1]})
            assert exact.completion.values == [pids[1]], exact
        finally:
            await client.call_structured("session_stop")
