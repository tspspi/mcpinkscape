# RC1 tool catalog

The MCP tool list is authoritative: a remote API key can omit tools according
to its policy. This catalog describes the RC1 families.

| Need | Tools |
| --- | --- |
| Backend and session state | `server_status`, `list_documents`, `create_document`, `open_document`, `save_document`, `close_document`, `get_document_info` |
| Managed Unix application | `inkscape_start`, `inkscape_status`, `inkscape_shutdown`, `inkscape_release` (requires application-control policy; remote keys default to disabled) |
| Structure and inspection | `create_layer`, `list_layers`, `list_objects`, `get_object`, `get_object_bounds`, `get_selection`, `select_objects`, `clear_selection` |
| Drawing | `create_shape`, `create_rectangle` (including rounded `rx`/`ry`), `create_ellipse`, `create_circle`, `create_line`, `create_polyline`, `create_polygon`, `create_path`, `create_text`, `set_text` |
| Appearance | `set_style`, `set_fill`, `set_stroke` (width/opacity/dash/cap/join/miter), `set_opacity`, `set_object_attribute`, `set_page_background`, `create_gradient`, `set_gradient_stops`, `apply_gradient` |
| Raster content | `import_image` and native-only `live_import_image` for configured-root PNG/JPEG assets, embedded by default and placed as typed SVG images |
| Geometry and structure | `move_objects`, `rotate_objects`, `scale_objects`, `set_object_transform`, `duplicate_objects`, `delete_objects`, `group_objects`, `ungroup_objects`, `raise_objects`, `lower_objects` |
| Visual feedback and files | `render_snapshot`, `list_snapshots`, `get_snapshot_base64`, `export_document` |
| Active window | `live_status`, `live_select`, `live_set_style`, `live_move`, `live_rotate`, `live_scale`, `live_render_snapshot`, and native-only `live_poll_changes` |
| Native document targets | `live_list_documents`; pass its `instance_id` and `document_id` together to subsequent live tools |
| Atomic native edits | `live_apply_batch` (explicit target/revision, 1–100 typed operations, 256 KiB, one undo step) |
| Native layout | `live_align_objects`, `live_distribute_objects` (explicit target/revision, geometric or visual bounds, atomic undo) |
| Native inspection and layers | `live_get_document_info`, `live_get_object`, `live_list_layers`, `live_create_layer`, `live_set_current_layer` |
| Native live drawing | `live_list_objects`, `live_create_shape`, `live_create_text`, `live_set_text`, `live_set_page_background`, `live_create_gradient`, `live_set_gradient_stops`, `live_apply_gradient`, `live_import_image`, `live_duplicate_objects`, `live_delete_objects`, `live_group_objects`, `live_ungroup_objects` (discoverable before connection in native/auto mode, then filtered by negotiated methods) |

Offline mutations accept `expected_revision` where listed by the tool schema.
Read the document's revision with `get_document_info`; on a conflict, do not
blindly retry—inspect the changed objects first. Colours use CSS-style values,
opacities are from 0 to 1, and transform distances use SVG user units.

`render_snapshot` accepts page/drawing render choices and returns a registered
PNG snapshot. `export_document` writes a relative destination under the
configured document root and supports `svg`, `plain-svg`, `png`, and `pdf`.

The live active-window commands are version-dependent. Ask `live_status` and
respect its advertised capabilities before issuing a live mutation. They work
on selected live object IDs and return a selected-ID receipt when `select-list`
is advertised; make the selection and mutation small, then render for visual
confirmation. Do not pass `expected_revision` to those CLI tools: they reject
it because they cannot observe a live document revision.

Do not emulate raster import with raw XML, raw data URIs, or arbitrary
Inkscape actions. The live bridge accepts only a private server-staged file;
it never accepts a user-provided local path.

When a native bridge is connected, every live mutation accepts
`expected_revision`; use the current bridge revision and treat a mismatch as a
conflict. The CLI compatibility backend cannot provide revision-safe live
creation. Native tools stay discoverable before connection in native/auto mode;
an unavailable bridge produces an explicit error.

Native live tools accept the optional pair `instance_id` and `document_id`.
Retain both from `live_list_documents` so reads, edits, revision checks, and
snapshots follow that document independently of focus. Bridge and MCP restarts
preserve an open document's handle. Close/reopen and GUI restart invalidate it;
enumerate again and confirm the intended document. CLI calls cannot accept this
pair. With neither value supplied, live calls retain focus-based behavior.

For collaborative work, retain both values returned by `live_poll_changes`:
`revision` for document mutations and `selection_generation` for selection
changes. Poll before a consequential mutation; if either changed unexpectedly,
inspect the live document rather than replaying a stale edit.

Native batch entries use `{method, params}` with the supported protocol method
names: `shape.create`, `text.create`, `text.set`, `layer.create`,
`object.align`, `object.distribute`, `object.set_style`, `object.move`, `object.rotate`, `object.scale`, `object.delete`,
`object.group`, `object.ungroup`, `object.raise`, `object.lower`, `gradient.create`,
`gradient.set_stops`, `gradient.apply`, and `document.set_background`. Supply
explicit IDs for creation. Style keys use the typed tool names (for example,
`font_size`, `stroke_width`), rather than raw CSS field names. Batch requests
cannot change targets/revisions per entry or invoke arbitrary methods/actions.
Each batch with edits has one revision and one undo entry; no-op batches retain
revision and undo/redo history. inspect the returned
operation receipts and render before accepting the result. On transport loss,
inspect state rather than replaying the batch.


## Precise native layout

Align: `object_ids`, `axis` x/y, `alignment` start/center/end, `reference`
selection/page/object, optional `reference_id` only for object, `bounds`
geometric/visual. A reference object stays fixed and cannot overlap the movers
through ancestry. Selection reference means the requested IDs' combined bounds.
Page reference means the root SVG viewport in document pixels.

Distribute: `object_ids`, `axis`, `spacing` gaps/centers, optional nonnegative
`gap` for gaps only, `unit` px/mm/cm/in/pt/pc, `bounds` geometric/visual. Units
other than px require an explicit gap. At least three IDs are required without
a gap, two with one. Sort is spatial and stable for ties, preserving tree order.
Automatic spacing keeps first/last fixed (automatic gaps can be negative for
overlapping objects); explicit gaps anchor the first and can extend the layout
past the page. No automatic grid snapping. Movers must have finite visible
bounds and be unlocked, with no ancestor/descendant pairs or duplicate IDs.
Noninvertible parent transforms reject required movement. Differences within
1e-9 document pixels count as zero. Identity operations preserve revision and
undo/redo history. The same operations work on earlier-created batch objects.

For “top-align panels with 8 mm clear gaps,” issue one `live_apply_batch` with
both target IDs and the observed revision:

```json
[
  {"method":"object.align","params":{"object_ids":["left","middle","right"],"axis":"y","alignment":"start","bounds":"visual"}},
  {"method":"object.distribute","params":{"object_ids":["left","middle","right"],"axis":"x","gap":8,"unit":"mm","bounds":"visual"}}
]
```

Individual tool receipts expose `moves` (IDs, dx/dy, before/after document-pixel
bounds), `changed`, revision, and resolved spacing. In a batch, inspect each
`operation_results` entry and then measure/render the final state. Read the
bridge method list before using these tools with an older running extension.
