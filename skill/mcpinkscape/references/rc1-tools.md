# RC1 tool catalog

The MCP tool list is authoritative: a remote API key can omit tools according
to its policy. This catalog describes the RC1 families.

| Need | Tools |
| --- | --- |
| Backend and session state | `server_status`, `list_documents`, `create_document`, `open_document`, `save_document`, `close_document`, `get_document_info` |
| Structure and inspection | `create_layer`, `list_layers`, `list_objects`, `get_object`, `get_object_bounds`, `get_selection`, `select_objects`, `clear_selection` |
| Drawing | `create_shape`, `create_rectangle` (including rounded `rx`/`ry`), `create_ellipse`, `create_circle`, `create_line`, `create_polyline`, `create_polygon`, `create_path`, `create_text`, `set_text` |
| Appearance | `set_style`, `set_fill`, `set_stroke` (width/opacity/dash/cap/join/miter), `set_opacity`, `set_object_attribute`, `set_page_background`, `create_gradient`, `set_gradient_stops`, `apply_gradient` |
| Raster content | `import_image` and native-only `live_import_image` for configured-root PNG/JPEG assets, embedded by default and placed as typed SVG images |
| Geometry and structure | `move_objects`, `rotate_objects`, `scale_objects`, `set_object_transform`, `duplicate_objects`, `delete_objects`, `group_objects`, `ungroup_objects`, `raise_objects`, `lower_objects` |
| Visual feedback and files | `render_snapshot`, `list_snapshots`, `get_snapshot_base64`, `export_document` |
| Active window | `live_status`, `live_select`, `live_set_style`, `live_move`, `live_rotate`, `live_scale`, `live_render_snapshot`, and native-only `live_poll_changes` |
| Native live drawing | `live_list_objects`, `live_create_shape`, `live_create_text`, `live_set_text`, `live_set_page_background`, `live_create_gradient`, `live_set_gradient_stops`, `live_apply_gradient`, `live_import_image`, `live_duplicate_objects`, `live_delete_objects`, `live_group_objects`, `live_ungroup_objects` (advertised only when the native bridge is connected) |

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
creation, so native-only live drawing tools will not be advertised without the
bridge.

For collaborative work, retain both values returned by `live_poll_changes`:
`revision` for document mutations and `selection_generation` for selection
changes. Poll before a consequential mutation; if either changed unexpectedly,
inspect the live document rather than replaying a stale edit.
