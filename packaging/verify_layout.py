"""Analytical bounds, native history and rendered layout checks on a private display."""
from __future__ import annotations

import asyncio
from io import BytesIO
import json
import math
import os
import time
import uuid

from PIL import Image
from mcpinkscape.errors import BatchValidationError, LiveBackendUnavailableError

from verify_batches import history_key
from verify_document_targets import focus_fixture_window
from verify_live_inspection import near, require


async def verify_layout(client, state, env, activate):
    root = state.stdio_service.document_root.root
    source = root / "layout.svg"
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
      xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" width="105.833333333333mm" height="79.375mm" viewBox="0 0 200 150">
      <g id="layer" inkscape:groupmode="layer" inkscape:label="Layout">
        <rect id="a" x="10" y="15" width="10" height="10" fill="#ff0000" stroke="#000000" stroke-width="2"/>
        <rect id="b" x="40" y="30" width="20" height="15" fill="#00aa00"/>
        <rect id="c" x="100" y="50" width="30" height="20" fill="#0000ff"/>
        <rect id="ref" x="150" y="100" width="30" height="20" fill="#aaaa00"/>
        <g id="nested" transform="translate(35,75) rotate(30) scale(2,0.5)">
          <rect id="rotated" x="0" y="0" width="12" height="8" fill="#ff00ff" stroke="#000000" stroke-width="2"/>
          <text id="text" x="0" y="25" font-size="8">Measured text</text>
        </g>
        <g id="singular" transform="translate(70,70) scale(0,1)"><rect id="flat" width="10" height="10"/></g>
        <g id="locked-parent" sodipodi:insensitive="true"><rect id="locked" x="5" y="5" width="5" height="5"/></g>
        <rect id="hidden" width="5" height="5" display="none"/>
        <g id="hidden-parent" display="none"><rect id="hidden-child" width="5" height="5"/></g>
        <g id="empty"/>
      </g></svg>''')
    started = (await client.call_tool("inkscape_start", {"path": "layout.svg"})).data
    documents = (await client.call_tool("live_list_documents", {})).data["documents"]
    target = {key: documents[0][key] for key in ("instance_id", "document_id")}
    pid = started["pid"]

    async def call(name, **params):
        return (await client.call_tool(name, {**target, **params})).data

    async def obj(id):
        return (await call("live_get_object", object_id=id))["object"]

    async def box(id, bounds="geometric"):
        return (await obj(id))[bounds + "_bounds"]

    async def revision():
        return (await call("live_status"))["revision"]

    async def edit(name, **params):
        before = await revision()
        result = await call(name, expected_revision=before, **params)
        require(result["revision"] == before + int(result["changed"]), "Layout revision/changed receipt disagrees")
        return result

    async def pixels():
        snapshot = await call("live_render_snapshot")
        image = Image.open(BytesIO(state.stdio_service.snapshots.get(snapshot["snapshot_id"]).path.read_bytes())).convert("RGBA")
        require(image.size == (400,300), "Layout physical page dimensions changed")
        return image

    async def until(predicate):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            if await predicate(): return
            await asyncio.sleep(0.1)
        raise RuntimeError("Layout undo/redo did not reach expected geometry")

    originals = {id: await obj(id) for id in ("a","b","c","ref","nested","rotated","text")}
    baseline = (await pixels()).tobytes()
    await call("live_select", object_ids=["a","b"], expected_revision=await revision())
    initial_layer = (await call("live_get_document_info"))["current_layer_id"]

    # Layout must stay bound while an equally revised different document has focus.
    (root/"layout-other.svg").write_text(source.read_text())
    application_id = state.stdio_service.application._record()["application_id"]
    activate("file-open-window", application_id, "[<" + json.dumps(str(root / "layout-other.svg")) + ">]")
    listed = (await client.call_tool("live_list_documents", {})).data["documents"]
    other = next(d for d in listed if d["label"].endswith("layout-other.svg"))
    focus_fixture_window(env["DISPLAY"],pid,"layout-other.svg")
    async def focused_other():
        return (await client.call_tool("live_poll_changes", {"after_revision":0,"after_selection_generation":0})).data["document_id"] == other["document_id"]
    await until(focused_other)
    result = await edit("live_align_objects", object_ids=["a","b","c"], axis="y", alignment="start")
    for id in ("a","b","c"): near((await box(id))["y"],30,"Selection top alignment")
    require(await focused_other(),"Layout stole window focus")
    other_status = (await client.call_tool("live_status", {key:other[key] for key in target})).data
    require(other_status["revision"] == 0,"Layout edited focused other document")
    require((await call("live_list_selection"))["object_ids"] == ["a","b"],"Layout changed selection")
    require((await call("live_get_document_info"))["current_layer_id"] == initial_layer,"Layout changed destination layer")
    require(result["bounds_space"] == "document_px" and len(result["moves"]) == 3,"Incomplete layout receipt")
    near((await box("a"))["x"],20,"Alignment changed perpendicular coordinate")
    rendered = await pixels()
    require(rendered.getpixel((100,40))[:3] == (0,170,0),"Aligned green panel pixels disagree with geometry")
    require(rendered.getpixel((100,80))[3] == 0,"Aligned panel left pixels at old position")
    async def restored(): return (await box("b")) == originals["b"]["geometric_bounds"] and (await box("c")) == originals["c"]["geometric_bounds"]
    history_key(env["DISPLAY"],pid,title="layout.svg")
    await until(restored)
    require((await pixels()).tobytes() == baseline,"One undo failed to restore all layout pixels")
    # A no-op between undo and redo must retain the existing redo history.
    no_op = await edit("live_align_objects",object_ids=["a"],axis="x",alignment="start")
    require(not no_op["changed"],"Identity alignment created history")
    history_key(env["DISPLAY"],pid,redo=True,title="layout.svg")
    async def redone(): return math.isclose((await box("c"))["y"],30,abs_tol=1e-6)
    await until(redone)
    history_key(env["DISPLAY"],pid,title="layout.svg");await until(restored)

    for axis,alignment,want in [("x","start",0),("x","center",190),("x","end",380),("y","start",0),("y","center",140),("y","end",280)]:
        await edit("live_align_objects", object_ids=["a"],axis=axis,alignment=alignment,reference="page")
        near((await box("a"))[axis],want,"Viewport alignment with physical units/viewBox")
    await edit("live_align_objects",object_ids=["a"],axis="x",alignment="start",reference="object",reference_id="ref",bounds="visual")
    near((await box("a","visual"))["x"],300,"Visual stroke alignment")
    require(await obj("ref") == originals["ref"],"Alignment moved fixed reference")
    await edit("live_align_objects",object_ids=["rotated","text"],axis="x",alignment="center",reference="page",bounds="visual")
    for id in ("rotated","text"):
        b=await box(id,"visual");near(b["x"]+b["width"]/2,200,"Rotated/scaled parent document-space alignment")
        for i in range(4):near((await obj(id))["local_transform"][i],originals[id]["local_transform"][i],"Layout changed rotation/scale")
    group_before=await box("nested")
    child_before=await box("rotated")
    await edit("live_align_objects",object_ids=["nested"],axis="y",alignment="end",reference="page")
    group=await box("nested");near(group["y"]+group["height"],300,"Group alignment")
    child_after=await box("rotated")
    near(child_after["width"],child_before["width"],"Group movement scaled child")
    near(child_after["y"]-child_before["y"],300-group_before["y"]-group_before["height"],"Group translation consistency")

    # Distribute unequal widths and verify both edge and center spacing.
    def op(method,**params): return {"method":method,"params":params}
    for spacing in ("gaps","centers"):
        result=await edit("live_distribute_objects",object_ids=["c","a","b"],axis="x",spacing=spacing)
        moves=result["moves"]
        require(moves[0]["dx"] == 0 and moves[-1]["dx"] == 0,"Automatic distribution moved endpoints")
        ordered=[await box(m["object_id"]) for m in moves]
        if spacing == "gaps":
            near(ordered[1]["x"]-ordered[0]["x"]-ordered[0]["width"],ordered[2]["x"]-ordered[1]["x"]-ordered[1]["width"],"Equal visible-edge gaps")
        else:
            centers=[b["x"]+b["width"]/2 for b in ordered]
            near(centers[1]-centers[0],centers[2]-centers[1],"Equal center spacing")
    for axis in ("x","y"):
        result=await edit("live_distribute_objects",object_ids=["c","a","b"],axis=axis,gap=8,unit="mm",bounds="visual")
        ordered=[await box(m["object_id"],"visual") for m in result["moves"]]
        size="width" if axis == "x" else "height"
        for left,right in zip(ordered,ordered[1:]):near(right[axis]-left[axis]-left[size],8*96/25.4,"Explicit 8 mm gap")
        require(result["moves"][0]["dx"] == result["moves"][0]["dy"] == 0,"Explicit gap moved first anchor")
    # Same operation again must preserve revision and undo history.
    repeat=await edit("live_distribute_objects",object_ids=["c","a","b"],axis="y",gap=8,unit="mm",bounds="visual")
    require(not repeat["changed"],"Repeated distribution is not a no-op")

    # Failure late in a batch must not move the earlier valid targets.
    before={id:await obj(id) for id in ("a","b","c","ref")};rev=await revision()
    bad_cases=[("live_align_objects",dict(object_ids=["a","locked"],axis="x",alignment="end",reference="page")),
        ("live_align_objects",dict(object_ids=["nested","rotated"],axis="y",alignment="start")),
        ("live_align_objects",dict(object_ids=["hidden"],axis="x",alignment="start")),
        ("live_align_objects",dict(object_ids=["hidden-child"],axis="x",alignment="start")),
        ("live_align_objects",dict(object_ids=["empty"],axis="x",alignment="start")),
        ("live_align_objects",dict(object_ids=["flat"],axis="x",alignment="end",reference="page")),
        ("live_align_objects",dict(object_ids=["nested"],axis="x",alignment="start",reference="object",reference_id="text")),
        ("live_apply_batch",dict(operations=[op("object.align",object_ids=["a"],axis="x",alignment="start",reference="page"),op("object.distribute",object_ids=["b","c","missing"],axis="x")]))]
    for name,params in bad_cases:
        result=await client.call_tool(name,{**target,"expected_revision":rev,**params},raise_on_error=False)
        require(result.is_error,"Invalid layout was accepted: "+str(params))
        require(await revision() == rev,"Rejected layout changed revision")
        for id in before:require(await obj(id) == before[id],"Rejected layout partially moved targets")
    # Raw native clients must meet the same identifier/schema rules as MCP;
    # notably an embedded NUL must not be truncated into an existing object ID.
    for params in ({"object_ids":["a\0suffix"],"axis":"x","alignment":"end","reference":"page"},
                   {"object_ids":["a"],"axis":"x","alignment":"start","reference":"object","reference_id":"ref\0suffix"},
                   {"object_ids":["a","a"],"axis":"x","alignment":"end"},
                   {"object_ids":["a"],"axis":"z","alignment":"start"}):
        try:
            state.stdio_service.native_bridge.request("object.align",params,rev,target=target)
        except (BatchValidationError, LiveBackendUnavailableError):
            pass
        else:
            raise RuntimeError("Malformed raw native layout was accepted")
        require(await revision() == rev,"Malformed raw layout changed revision")
        for id in before:require(await obj(id) == before[id],"Malformed raw layout changed objects")
    stale=await client.call_tool("live_align_objects",{**target,"expected_revision":rev+1,"object_ids":["a"],"axis":"x","alignment":"start"},raise_on_error=False)
    require(stale.is_error,"Layout ignored stale revision")
    stale=await client.call_tool("live_align_objects",{**target,"document_id":str(uuid.uuid4()),"expected_revision":rev,"object_ids":["a"],"axis":"x","alignment":"start"},raise_on_error=False)
    require(stale.is_error,"Layout ignored stale document target")
    batch=await edit("live_apply_batch",operations=[op("layer.create",layer_id="new-layout",label="Layout batch"),
        op("shape.create",kind="rectangle",object_id="new-panel",layer_id="new-layout",values={"x":5,"y":5,"width":10,"height":10}),
        op("object.align",object_ids=["new-panel"],axis="x",alignment="end",reference="page"),
        op("object.distribute",object_ids=["new-panel","a","b"],axis="y",gap=3,unit="mm")])
    require(batch["changed"] and len(batch["operation_results"]) == 4,"Dependent layout batch did not commit")
    near((await box("new-panel"))["x"],380,"Batch failed to measure earlier created object")
    history_key(env["DISPLAY"],pid,title="layout.svg")
    async def batch_undone():
        return "new-layout" not in {layer["id"] for layer in (await call("live_list_layers"))["layers"]}
    await until(batch_undone)
    for id in before:require(await obj(id) == before[id],"One undo did not restore dependent layout batch")
    await client.call_tool("inkscape_shutdown",{"save_mode":"discard"})

    # Exercise actual live rollback after layout XML writes, not just detached
    # admission failure. The flag exists only in the private test environment.
    previous=os.environ.get("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER")
    os.environ["MCPINKSCAPE_TEST_BATCH_FAIL_AFTER"]="1"
    try:
        await client.call_tool("inkscape_start",{"path":"layout.svg"})
    finally:
        if previous is None:os.environ.pop("MCPINKSCAPE_TEST_BATCH_FAIL_AFTER",None)
        else:os.environ["MCPINKSCAPE_TEST_BATCH_FAIL_AFTER"]=previous
    documents=(await client.call_tool("live_list_documents",{})).data["documents"]
    target={key:documents[0][key] for key in target}
    failed=await client.call_tool("live_apply_batch",{**target,"expected_revision":0,"operations":[
        op("object.align",object_ids=["a","b","c"],axis="y",alignment="end",reference="page")]},raise_on_error=False)
    require(failed.is_error and "rolled back" in str(failed),"Layout execution fault did not roll back")
    for id in originals:require(await obj(id) == originals[id],"Execution rollback left layout geometry or transforms changed")
    require(await revision() == 0 and (await pixels()).tobytes() == baseline,"Layout rollback changed revision or pixels")
    require(not (await client.call_tool("live_list_documents",{})).data["documents"][0]["modified"],"Layout rollback dirtied a clean document")
    await client.call_tool("inkscape_shutdown",{"save_mode":"discard"})
    return {"selection_page_reference_alignment":True,"geometric_visual_bounds":True,"document_space_nested_transforms":True,
        "groups_and_text":True,"equal_gaps_and_centers":True,"physical_gap_units":True,"stable_spatial_order":True,
        "focus_target_selection_preserved":True,"single_undo_redo_and_pixels":True,"no_op_preserves_history":True,
        "prevalidation_and_stale_rejection":True,"dependent_atomic_batches":True,"execution_failure_rollback":True}
