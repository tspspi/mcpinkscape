"""Document-targeting checks for the private GUI lifecycle fixture."""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def focus_fixture_window(display_name, pid, label, window_id=None, close=False):
    """Focus a specifically named window of our private fixture process only."""
    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    pointer, window = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], pointer
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [pointer], window
    x11.XQueryTree.argtypes = [pointer, window, ctypes.POINTER(window), ctypes.POINTER(window),
                              ctypes.POINTER(ctypes.POINTER(window)), ctypes.POINTER(ctypes.c_uint)]
    x11.XInternAtom.argtypes, x11.XInternAtom.restype = [pointer, ctypes.c_char_p, ctypes.c_int], window
    x11.XGetWindowProperty.argtypes = [pointer, window, window, ctypes.c_long, ctypes.c_long, ctypes.c_int,
        window, ctypes.POINTER(window), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(pointer)]
    x11.XFetchName.argtypes = [pointer, window, ctypes.POINTER(ctypes.c_char_p)]
    x11.XFree.argtypes = [pointer]
    x11.XRaiseWindow.argtypes = [pointer, window]
    x11.XSetInputFocus.argtypes = [pointer, window, ctypes.c_int, ctypes.c_ulong]
    x11.XWarpPointer.argtypes = [pointer, window, window, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
    x11.XSync.argtypes, x11.XCloseDisplay.argtypes = [pointer, ctypes.c_int], [pointer]
    display = x11.XOpenDisplay(display_name.encode())
    require(display, "Cannot open private display")
    children = ctypes.POINTER(window)()
    try:
        root, parent, count = window(), window(), ctypes.c_uint()
        require(x11.XQueryTree(display, x11.XDefaultRootWindow(display), ctypes.byref(root),
                              ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)), "Cannot query fixture windows")
        atom = x11.XInternAtom(display, b"_NET_WM_PID", 0)
        titles = []
        for index in range(count.value):
            actual, format_, items, remaining, value = window(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), pointer()
            x11.XGetWindowProperty(display, children[index], atom, 0, 1, 0, 0, ctypes.byref(actual),
                ctypes.byref(format_), ctypes.byref(items), ctypes.byref(remaining), ctypes.byref(value))
            owned = bool(value and items.value == 1 and format_.value == 32 and
                         ctypes.cast(value, ctypes.POINTER(window))[0] == pid)
            if value:
                x11.XFree(value)
            if not owned:
                continue
            name = ctypes.c_char_p()
            x11.XFetchName(display, children[index], ctypes.byref(name))
            titles.append(name.value.decode(errors="replace") if name.value else "")
            matches = (children[index] == window_id if window_id is not None else
                       bool(name.value and label in name.value.decode(errors="replace")))
            if name:
                x11.XFree(ctypes.cast(name, pointer))
            if matches:
                if close:
                    class ClientMessage(ctypes.Structure):
                        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                            ("send_event", ctypes.c_int), ("display", pointer), ("window", window),
                            ("message_type", window), ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]
                    event = (ctypes.c_long * 24)()
                    message = ctypes.cast(event, ctypes.POINTER(ClientMessage)).contents
                    message.type, message.display, message.window, message.format = 33, display, children[index], 32
                    message.message_type = x11.XInternAtom(display, b"WM_PROTOCOLS", 0)
                    message.data[0] = x11.XInternAtom(display, b"WM_DELETE_WINDOW", 0)
                    x11.XSendEvent.argtypes = [pointer, window, ctypes.c_int, ctypes.c_long, pointer]
                    require(x11.XSendEvent(display, children[index], 0, 0, ctypes.byref(event)), "Cannot close fixture window")
                else:
                    x11.XRaiseWindow(display, children[index])
                    x11.XSetInputFocus(display, children[index], 1, 0)
                    x11.XWarpPointer(display, 0, children[index], 0, 0, 0, 0, 30, 100)
                x11.XSync(display, 0)
                return int(children[index])
        raise RuntimeError(f"No owned fixture window matches {label}; owned window titles: {titles}")
    finally:
        if children:
            x11.XFree(children)
        x11.XCloseDisplay(display)


async def verify_document_targets(client, state, configuration, env, activate, temporary, log):
    root = state.stdio_service.document_root.root
    for name, size, color in (("target-a.svg", 64, "#ff0000"), ("target-b.svg", 96, "#0000ff")):
        (root / name).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
            f'<rect id="shared" x="4" y="4" width="20" height="20" fill="{color}"/></svg>')
    started = (await client.call_tool("inkscape_start", {"path": "target-a.svg"})).data
    application_id = state.stdio_service.application._record()["application_id"]
    activate("file-open-window", application_id, "[<" + json.dumps(str(root / "target-b.svg")) + ">]")

    async def documents():
        return (await client.call_tool("live_list_documents", {})).data

    async def find_two():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = await documents()
            found = {Path(item["label"]).name: item for item in result["documents"]}
            if "target-a.svg" in found and "target-b.svg" in found:
                return result, found
            await asyncio.sleep(0.1)
        raise RuntimeError("Two fixture documents did not appear")

    listed, found = await find_two()
    a = {key: found["target-a.svg"][key] for key in ("instance_id", "document_id")}
    b = {key: found["target-b.svg"][key] for key in ("instance_id", "document_id")}
    require(a != b and a["instance_id"] == b["instance_id"], "Document IDs are not distinct within an instance")
    require(found["target-a.svg"]["revision"] == found["target-b.svg"]["revision"] == 0,
            "Expected identical initial revisions")
    (root / "target-no-window.svg").write_text((root / "target-b.svg").read_text())
    activate("file-open", application_id, "[<" + json.dumps(str(root / "target-no-window.svg")) + ">]")
    hidden = next(item for item in (await documents())["documents"] if item["label"].endswith("target-no-window.svg"))
    require(not hidden["desktop_available"], "Non-window document unexpectedly has a desktop")
    unavailable = await client.call_tool("live_list_objects", {key: hidden[key] for key in ("instance_id", "document_id")},
                                        raise_on_error=False)
    require(unavailable.is_error, "Target without a desktop silently fell back to another document")
    activate("file-close", application_id)

    fixture_windows = {}

    async def focus(name, target):
        fixture_windows[name] = focus_fixture_window(env["DISPLAY"], started["pid"], name, fixture_windows.get(name))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = (await client.call_tool("live_poll_changes", {"after_revision": 0, "after_selection_generation": 0})).data
            if current["document_id"] == target["document_id"]:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Fixture focus did not move to " + name)

    # Both targets contain the same object ID and revision. Focus B, edit A.
    await focus("target-a.svg", a)
    await focus("target-b.svg", b)
    changed = (await client.call_tool("live_set_style", {**a, "object_ids": ["shared"],
        "style": {"fill": "#00ff00"}, "expected_revision": 0})).data
    require(changed["document_id"] == a["document_id"], "Mutation receipt identifies the wrong document")
    active = (await client.call_tool("live_poll_changes", {"after_revision": 0, "after_selection_generation": 0})).data
    require(active["document_id"] == b["document_id"], "Bound mutation changed window focus")
    require(focus_fixture_window(env["DISPLAY"], started["pid"], "target-b.svg") == fixture_windows["target-b.svg"],
            "Background mutation changed the focused window's document title")
    require((await client.call_tool("live_status", b)).data["revision"] == 0, "Bound edit touched the focused other document")
    await client.call_tool("live_select", {**a, "object_ids": ["shared"], "expected_revision": 1})
    require((await client.call_tool("live_list_selection", a)).data["object_ids"] == ["shared"], "Wrong bound selection")
    require((await client.call_tool("live_list_selection", b)).data["object_ids"] == [], "Selection leaked into other document")
    conflict = await client.call_tool("live_select", {**b, "object_ids": ["shared"], "expected_revision": 1}, raise_on_error=False)
    require(conflict.is_error, "Selection ignored the target document revision")
    invalid = await client.call_tool("live_select", {**b, "object_ids": ["shared"], "expected_revision": -1}, raise_on_error=False)
    require(invalid.is_error, "Selection accepted an invalid revision")
    for target, size in ((a, 64), (b, 96)):
        snapshot = (await client.call_tool("live_render_snapshot", {**target, "area": "page"})).data
        require(snapshot["document_id"] == target["document_id"], "Snapshot receipt target mismatch")
        png = state.stdio_service.snapshots.get(snapshot["snapshot_id"]).path.read_bytes()
        require(struct.unpack(">II", png[16:24]) == (size, size), "Snapshot rendered the focused document instead of its target")
    await focus("target-a.svg", a)
    require((await client.call_tool("live_status", b)).data["revision"] == 0, "Targeted read followed changed focus")

    activate("org.mcpinkscape.bridge.stop", application_id)
    _, after_bridge = await find_two()
    require(after_bridge["target-a.svg"]["document_id"] == a["document_id"] and
            after_bridge["target-b.svg"]["document_id"] == b["document_id"], "Bridge restart replaced document identities")

    config_path = temporary / "target-reconnect.conf"
    config_path.write_text(configuration.model_dump_json())
    worker = r"""
import asyncio,json,sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
async def run():
    async with Client(StdioTransport(sys.executable,["-m","mcpinkscape","--config",sys.argv[1]],keep_alive=False)) as c:
        target=json.loads(sys.argv[2])
        listed=(await c.call_tool("live_list_documents",{})).data
        assert any(d["document_id"]==target["document_id"] for d in listed["documents"])
        status=(await c.call_tool("live_status",target)).data
        assert status["instance_id"]==target["instance_id"] and status["document_id"]==target["document_id"]
asyncio.run(run())
"""
    resumed = subprocess.run([sys.executable, "-c", worker, str(config_path), json.dumps(a)],
                             env=env, capture_output=True, text=True, timeout=45)
    log.write(resumed.stderr)
    log.flush()
    require(resumed.returncode == 0, "Target did not survive MCP-process restart: " + resumed.stderr)

    await focus("target-b.svg", b)
    # Use the normal close event: Inkscape's scripted window-close action
    # deliberately keeps the document loaded without a window.
    focus_fixture_window(env["DISPLAY"], started["pid"], "target-b.svg", fixture_windows["target-b.svg"], close=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(item["document_id"] != b["document_id"] for item in (await documents())["documents"]):
            break
        await asyncio.sleep(0.1)
    require(all(item["document_id"] != b["document_id"] for item in (await documents())["documents"]),
            "Normal window closure did not close its document")
    closed = await client.call_tool("live_list_objects", b, raise_on_error=False)
    require(closed.is_error, "Closed document target was accepted")
    from mcpinkscape.errors import StaleDocumentTargetError
    try:
        state.stdio_service.live_render_snapshot(**b)
    except StaleDocumentTargetError:
        pass
    else:
        raise RuntimeError("Closed snapshot target did not report stale_document_target")
    activate("file-open-window", application_id, "[<" + json.dumps(str(root / "target-b.svg")) + ">]")
    _, reopened = await find_two()
    require(reopened["target-b.svg"]["document_id"] != b["document_id"], "Reopened document reused its old ID")
    stale = await client.call_tool("live_set_style", {**b, "object_ids": ["shared"],
        "style": {"fill": "#ff00ff"}, "expected_revision": 0}, raise_on_error=False)
    require(stale.is_error, "Stale target edited a reopened document with the same object ID and revision")
    saved = (await client.call_tool("inkscape_shutdown", {})).data["saved_paths"]
    require(any("#00ff00" in (root / path).read_text() for path in saved), "Bound edit was not saved in its target")
    await client.call_tool("inkscape_start", {"path": "target-a.svg"})
    restarted = await documents()
    require(restarted["instance_id"] != a["instance_id"], "GUI restart reused application instance identity")
    stale = await client.call_tool("live_list_objects", a, raise_on_error=False)
    require(stale.is_error, "Old application target was accepted after restart")
    await client.call_tool("inkscape_shutdown", {"save_mode": "discard"})
    return {"two_documents_same_revision": True, "focus_independent_edit": True,
            "focus_and_window_titles_preserved": True, "document_without_desktop_rejected": True,
            "bound_selection_and_revision": True, "bound_snapshot": True, "bridge_identity_preserved": True,
            "mcp_identity_preserved": True, "closed_target_rejected": True, "reopened_target_rejected": True,
            "application_restart_invalidates_target": True}
