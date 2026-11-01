"""Atomic-batch qualification in the lifecycle verifier's private GUI session."""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
from io import BytesIO
import os
import time
import json
import uuid

from PIL import Image

from verify_document_targets import focus_fixture_window


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def history_key(display_name, pid, redo=False):
    # Window ownership is checked before sending keys on the private display.
    focus_fixture_window(display_name, pid, "batch.svg")
    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    xtst = ctypes.CDLL(ctypes.util.find_library("Xtst"))
    pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], pointer
    x11.XStringToKeysym.argtypes, x11.XStringToKeysym.restype = [ctypes.c_char_p], ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes, x11.XKeysymToKeycode.restype = [pointer, ctypes.c_ulong], ctypes.c_ubyte
    x11.XSync.argtypes, x11.XCloseDisplay.argtypes = [pointer, ctypes.c_int], [pointer]
    xtst.XTestFakeKeyEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(display_name.encode())
    require(display, "Cannot open private batch display")
    try:
        ctrl, shift, z = [x11.XKeysymToKeycode(display, x11.XStringToKeysym(name)) for name in (b"Control_L", b"Shift_L", b"z")]
        sequence = [(ctrl, 1)] + ([(shift, 1)] if redo else []) + [(z, 1), (z, 0)] + ([(shift, 0)] if redo else []) + [(ctrl, 0)]
        for key, pressed in sequence:
            require(xtst.XTestFakeKeyEvent(display, key, pressed, 0), "Batch history key failed")
        x11.XSync(display, 0)
    finally:
        x11.XCloseDisplay(display)


async def verify_batches(client, state, env, activate):
    root = state.stdio_service.document_root.root
    source = root / "batch.svg"
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="128" height="96">
      <g id="layer" inkscape:groupmode="layer" inkscape:label="Batch">
        <rect id="keep" x="5" y="5" width="15" height="15" fill="#00aa00"/>
        <rect id="delete" x="25" y="5" width="10" height="10" fill="#aaaa00"/>
        <text id="label" x="5" y="70" font-size="10">before</text>
      </g></svg>''')

    async def start(fault=False):
        previous = os.environ.get("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER")
        if fault:
            os.environ["MCPINKSCAPE_TEST_BATCH_FAIL_AFTER"] = "2"
        else:
            os.environ.pop("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER", None)
        try:
            started = (await client.call_tool("inkscape_start", {"path": "batch.svg"})).data
        finally:
            if previous is None:
                os.environ.pop("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER", None)
            else:
                os.environ["MCPINKSCAPE_TEST_BATCH_FAIL_AFTER"] = previous
        documents = (await client.call_tool("live_list_documents", {})).data["documents"]
        require(len(documents) == 1, "Batch fixture should have one document")
        return started["pid"], {key: documents[0][key] for key in ("instance_id", "document_id")}

    pid, target = await start()

    async def call(name, **params):
        return (await client.call_tool(name, {**target, **params})).data

    async def object_(id):
        return (await call("live_get_object", object_id=id))["object"]

    async def pixels():
        snapshot = await call("live_render_snapshot")
        return Image.open(BytesIO(state.stdio_service.snapshots.get(snapshot["snapshot_id"]).path.read_bytes())).convert("RGBA").tobytes()

    async def wait_until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if await predicate():
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Batch GUI history did not reach expected state")

    async def is_original():
        children = (await object_("layer"))["child_ids"]
        return "batch-layer" not in children and "delete" in children and (await object_("keep"))["effective_style"]["fill"] == "#00aa00" and (await object_("label"))["text"] == "before"

    def op(method, **params):
        return {"method": method, "params": params}

    await call("live_set_current_layer", layer_id="layer", expected_revision=0)
    await call("live_select", object_ids=["keep", "delete"], expected_revision=0)
    baseline = await object_("keep")
    baseline_pixels = await pixels()
    (root / "batch-other.svg").write_text(source.read_text())
    application_id = state.stdio_service.application._record()["application_id"]
    activate("file-open-window", application_id, "[<" + json.dumps(str(root / "batch-other.svg")) + ">]")
    listed = (await client.call_tool("live_list_documents", {})).data["documents"]
    other = next(item for item in listed if item["label"].endswith("batch-other.svg"))
    focus_fixture_window(env["DISPLAY"], pid, "batch-other.svg")
    async def other_is_focused():
        return (await client.call_tool("live_poll_changes", {"after_revision": 0, "after_selection_generation": 0})).data["document_id"] == other["document_id"]
    await wait_until(other_is_focused)
    stale_target = await client.call_tool("live_apply_batch", {**target, "document_id": str(uuid.uuid4()), "expected_revision": 0,
        "operations": [op("object.delete", object_ids=["delete"])]}, raise_on_error=False)
    require(stale_target.is_error, "Batch accepted a stale document target")
    invalid = await client.call_tool("live_apply_batch", {**target, "expected_revision": 0, "operations": [
        op("object.set_style", object_ids=["keep"], style={"fill": "#ff0000"}),
        op("object.move", object_ids=["missing-later"], dx=1, dy=2)]}, raise_on_error=False)
    require(invalid.is_error, "Invalid later operation was accepted")
    require(await object_("keep") == baseline and await pixels() == baseline_pixels, "Preflight changed the live document")
    require((await call("live_status"))["revision"] == 0, "Rejected batch changed revision")
    stale = await client.call_tool("live_apply_batch", {**target, "expected_revision": 1, "operations": [op("object.delete", object_ids=["delete"])]}, raise_on_error=False)
    require(stale.is_error and (await object_("delete"))["id"] == "delete", "Batch ignored stale revision")
    # A batch cannot delete the SVG root or overlap ancestor/descendant deletes.
    invalid_tree = await client.call_tool("live_apply_batch", {**target, "expected_revision": 0, "operations": [op("object.delete", object_ids=["layer", "keep"])]}, raise_on_error=False)
    require(invalid_tree.is_error and await object_("keep") == baseline, "Overlapping deletion was accepted")
    operations = [
        op("layer.create", layer_id="batch-layer", label="Batch layer", parent_layer_id="layer"),
        op("shape.create", kind="rectangle", object_id="new-rect", layer_id="batch-layer", values={"x": 40, "y": 10, "width": 10, "height": 10}),
        op("object.move", object_ids=["new-rect"], dx=5, dy=3),
        op("object.set_style", object_ids=["keep"], style={"fill": "#ff0000"}),
        op("text.set", object_id="label", text="after"),
        op("gradient.create", kind="linear", gradient_id="batch-gradient", stops=[{"offset": 0, "colour": "#0000ff"}, {"offset": 1, "colour": "#ffffff"}]),
        op("gradient.apply", object_ids=["new-rect"], gradient_id="batch-gradient"),
        op("object.group", object_ids=["new-rect"], group_id="batch-group"),
        op("object.ungroup", group_id="batch-group"),
        op("object.delete", object_ids=["delete"]),
    ]
    result = await call("live_apply_batch", operations=operations, expected_revision=0)
    require(result["revision"] == 1 and result["operation_count"] == len(operations), "Batch did not commit one revision")
    require(len(result["operation_results"]) == len(operations) and {"keep", "delete", "new-rect", "batch-layer", "label", "batch-gradient"} <= set(result["affected_ids"]), "Incomplete batch receipt")
    require({"new-rect", "batch-group"} <= set(result["operation_results"][7]["affected_ids"]) and "batch-group" in result["operation_results"][8]["affected_ids"], "Structural receipt omitted affected IDs")
    require((await object_("new-rect"))["geometric_bounds"]["x"] == 45, "Dependent batch edit failed")
    require((await call("live_list_selection"))["object_ids"] == ["keep"], "Batch failed to preserve surviving selection")
    require((await call("live_get_document_info"))["current_layer_id"] == "layer", "Batch changed destination layer")
    require(await pixels() != baseline_pixels, "Successful batch did not render changes")
    require(await other_is_focused(), "Batch changed window focus")
    other_target = {key: other[key] for key in ("instance_id", "document_id")}
    require((await client.call_tool("live_status", other_target)).data["revision"] == 0, "Batch edited the focused other document")
    history_key(env["DISPLAY"], pid)
    await wait_until(is_original)
    require(await pixels() == baseline_pixels, "One undo did not restore all batch pixels")
    gradient_gone = await client.call_tool("live_get_object", {**target, "object_id": "batch-gradient"}, raise_on_error=False)
    require(gradient_gone.is_error, "One undo left batch definitions behind")
    history_key(env["DISPLAY"], pid, redo=True)
    async def is_redone():
        return "batch-layer" in (await object_("layer"))["child_ids"] and (await object_("label"))["text"] == "after"
    await wait_until(is_redone)
    require((await object_("keep"))["effective_style"]["fill"] == "#ff0000", "One redo did not restore batch styling")
    await client.call_tool("inkscape_shutdown", {"save_mode": "discard"})

    # Restart only this disposable application with native fault injection. Make
    # a redo entry before failing a batch so rollback must preserve real history.
    pid, target = await start(fault=True)
    await call("live_set_current_layer", layer_id="layer", expected_revision=0)
    await call("live_select", object_ids=["keep", "delete"], expected_revision=0)
    clean_layer = await object_("layer")
    clean_failure = await client.call_tool("live_apply_batch", {**target, "expected_revision": 0, "operations": [
        op("shape.create", kind="circle", object_id="rolled-back-creation", values={"cx": 80, "cy": 50, "r": 3}),
        op("object.delete", object_ids=["delete"]),
        op("text.set", object_id="label", text="never applied")]}, raise_on_error=False)
    require(clean_failure.is_error and "rolled back" in str(clean_failure), "Clean batch fault was not injected")
    require(await object_("layer") == clean_layer, "Creation/deletion rollback changed layer structure")
    require(not (await client.call_tool("live_list_documents", {})).data["documents"][0]["modified"], "Rollback marked a clean document modified")
    require((await call("live_status"))["revision"] == 0, "Clean rollback changed revision")
    await call("live_move", object_ids=["keep"], dx=1, dy=0, expected_revision=0)
    history_key(env["DISPLAY"], pid)
    async def move_is_undone():
        return (await object_("keep"))["geometric_bounds"]["x"] == 5
    await wait_until(move_is_undone)
    before = {id: await object_(id) for id in ("keep", "delete", "layer", "label")}
    before_pixels = await pixels()
    before_poll = await call("live_poll_changes", after_revision=0, after_selection_generation=0)
    no_op = await call("live_apply_batch", operations=[op("text.set", object_id="label", text="before")], expected_revision=before_poll["revision"])
    require(not no_op["changed"] and no_op["revision"] == before_poll["revision"], "No-op batch changed revision or history")
    before_modified = (await client.call_tool("live_list_documents", {})).data["documents"][0]["modified"]
    failed = await client.call_tool("live_apply_batch", {**target, "expected_revision": before_poll["revision"], "operations": [
        op("object.delete", object_ids=["delete"]),
        op("object.set_style", object_ids=["keep"], style={"fill": "#990099"}),
        op("shape.create", kind="circle", object_id="never-created", values={"cx": 60, "cy": 40, "r": 4})]}, raise_on_error=False)
    require(failed.is_error and "rolled back" in str(failed), "Injected native failure did not roll back")
    for id, expected in before.items():
        require(await object_(id) == expected, "Rollback changed object or stacking structure: " + id)
    require(await pixels() == before_pixels, "Rollback changed rendered document")
    require(sorted((await call("live_list_selection"))["object_ids"]) == ["delete", "keep"], "Rollback lost deleted selected object")
    after_poll = await call("live_poll_changes", after_revision=0, after_selection_generation=0)
    require(after_poll["revision"] == before_poll["revision"] and after_poll["selection_generation"] == before_poll["selection_generation"], "Rollback changed collaborative state")
    require((await client.call_tool("live_list_documents", {})).data["documents"][0]["modified"] == before_modified, "Rollback changed modified flag")
    require((await call("live_get_document_info"))["current_layer_id"] == "layer", "Rollback changed layer destination")
    history_key(env["DISPLAY"], pid, redo=True)
    async def move_is_redone():
        return (await object_("keep"))["geometric_bounds"]["x"] == 6
    await wait_until(move_is_redone)
    require((await object_("delete"))["id"] == "delete", "Preserved redo replayed failed batch")
    current_revision = (await call("live_status"))["revision"]
    # One operation does not hit the configured second-operation fault. Its
    # subsequent undo also checks that rollback left no pending XML undo debris.
    await call("live_apply_batch", operations=[op("text.set", object_id="label", text="next edit")], expected_revision=current_revision)
    history_key(env["DISPLAY"], pid)
    async def next_edit_is_undone():
        return (await object_("label"))["text"] == "before"
    await wait_until(next_edit_is_undone)
    require((await object_("keep"))["geometric_bounds"]["x"] == 6 and (await object_("delete"))["id"] == "delete", "Failed batch contaminated the next undo")
    await client.call_tool("inkscape_shutdown", {"save_mode": "discard"})
    return {"full_prevalidation": True, "focus_independent_batch": True, "stale_target": True, "clean_document_rollback": True, "stale_revision": True, "dependent_operations": True,
            "one_revision_and_receipts": True, "selection_and_layer_preserved": True,
            "single_undo_redo": True, "injected_failure_rollback": True, "rollback_pixels_and_structure": True,
            "rollback_collaborative_state": True, "existing_redo_preserved": True, "no_op_preserves_history": True, "next_undo_uncontaminated": True}
