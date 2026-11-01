---
name: mcpinkscape
description: Create, inspect, edit, render, verify, and design SVG/Inkscape communications through the mcpInkscape MCP server. Use for posters, scientific figures, brochures, and offline or collaborative local Inkscape work; do not use for arbitrary shell execution or non-SVG design tools.
---

# mcpInkscape

Use this skill when a task involves drawing or editing an SVG through the
mcpInkscape MCP server. Prefer its typed tools over raw SVG text or arbitrary
Inkscape actions, so the document remains inspectable and edits are
collaboration-safe.

For a communication-design task, begin by identifying the intended reader,
format, main message, required facts, and delivery stage. Read
[the Inkscape design guide](references/design-for-inkscape.md) before creating
a substantial poster, scientific figure/poster, leaflet, brochure, presentation
folder, or digital companion. Read [the delivery and review guide](references/design-review.md)
when preparing an export, assessing a render, or discussing print readiness,
accessibility, or a final handoff.

## Choose a backend

- Use the offline document tools for deterministic document creation and
  editing without a GUI. Work only below the server's configured document root.
- Use `live_*` tools when `server_status` reports a native bridge or supported
  active-window backend and the user has authorized the drawing work.
- A native bridge, when installed, is optional. Its absence must not block the
  offline or advertised active-window workflow.

For an agent-managed GUI, call `inkscape_start`; it opens the configured matched
application and enables the bridge without a menu action. Repeated starts attach
without duplicating the application. MCP/bridge reconnects preserve the running
document; never replay a mutation after losing its response.

When finished, `inkscape_shutdown` saves modified SVG copies under the configured
document root and returns their paths. Use `save_mode: "refuse"` to leave unsaved
work open. Explicit discard needs authorization to lose that work. For an
interactive handoff, `inkscape_release` leaves the GUI open and relinquishes
shutdown ownership. An attached user-launched GUI cannot be closed through these
methods. `inkscape_status` reports ownership and modified documents; a restarted
MCP process can recover a managed session's ownership without relaunching it.

After a native application upgrade, an already running GUI still has its loaded
bridge. Check the native bridge method list in `server_status` before using newer
tools. Save the managed session's work before restarting it, then enumerate
documents again because the application restart invalidates previous targets.
Leave a user-owned GUI under the user's control; replacing application files
does not upgrade its session.

For native live work, enumerate with `live_list_documents` and retain the intended
document's `instance_id` and `document_id`. Pass both to every live inspection,
selection, edit, revision poll, and snapshot; focus can change during collaborative
work. The pair survives bridge/MCP reconnects while that document stays open.
Close/reopen or application restart invalidates it: enumerate again and confirm
the intended document before continuing. A stale handle must never be replaced
silently. Older bridges and the CLI backend cannot accept explicit targets.

Inspect native geometry with `live_get_document_info`, `live_get_object`, and
`live_list_layers`. Bounds are in document pixels; drawing values are in the
destination parent's SVG user space. Use the returned affine matrices for
transformed groups and viewBoxes rather than assuming equal units. Check both
geometric and visual bounds when stroke or filters affect spacing; text bounds
come from Inkscape's layout. Effective style fields cover paint, opacity, and
basic typography rather than every CSS property.

For precise live layouts, use `live_align_objects` and `live_distribute_objects`
with the same explicit target/revision. Pick `bounds: "visual"` for visible
stroke/filter spacing, or `"geometric"` for shape geometry. Axis x starts/ends
are left/right; y starts/ends are top/bottom. Align to the requested objects'
combined bounds (`reference: "selection"`), root SVG viewport (`"page"`), or
an independent fixed `reference_id` (`"object"`). Page refers to the root SVG
viewport, not a selected page in a multi-page document. Distribution sorts by
position; automatic edge/center spacing keeps endpoints fixed. An explicit
`gap` anchors only the first object; specify `unit: "mm"` for physical spacing.
These calculations use document pixels even inside transformed groups. Inspect
`moves`/final bounds and render the result. Combine `object.align` and
`object.distribute` in `live_apply_batch` for one layout undo step. See the
[tool reference](references/rc1-tools.md) for constraints and a batch example.


Create native layers with `live_create_layer`, optionally beneath a parent layer.
Pass `layer_id` when creating shapes/text, or set the default with
`live_set_current_layer` and the observed revision. That default also controls
image import and does not clear the current selection. Keep using the explicit
document target for inspection, drawing, and rendered checks.

Use native `live_apply_batch` when a sequence of typed edits must succeed
atomically. Supply an explicit document target, one observed revision, and
1–100 operations within 256 KiB. Give newly created objects explicit IDs so later
operations can refer to them. The sequence is prevalidated and a successful
batch with changes produces one revision and one undo step, with ordered receipts
and affected IDs. A no-op reports `changed: false` without advancing revision or
changing undo/redo history. Check those receipts, then render the same document.

Validation errors leave live state untouched; reported execution errors roll
back document changes, selection, modified flags, and revision while retaining
existing redo history. Inspect before correcting a failed batch. If the response
is lost, do not replay: first determine whether the batch committed. Image
import, duplication, selection, and destination-layer changes remain standalone
calls. A batch preserves surviving selected IDs and an existing destination layer;
if it deletes that layer, the destination becomes the root.

## Working method

1. Call `server_status`, then create/open a document and inspect its revision.
2. Use `create_layer`, the typed `create_*` / `create_text` tools, and stable
   object IDs. Use `list_objects`, `get_object`, and `get_object_bounds` to
   verify intermediate state.
3. Apply `set_fill`, `set_stroke`, `set_opacity`, `set_text`, and the transform
   tools (`move_objects`, `rotate_objects`, `scale_objects`) with the latest
   `expected_revision` whenever another editor may be collaborating.
4. Treat graphical feedback as required: after every consequential layout,
   typography, figure, style, or transform change, call `render_snapshot` and
   inspect or retrieve it with `get_snapshot_base64`. Save/export only after
   the rendered document result is accepted.

For a substantial design, work in this order:

1. Record confirmed constraints separately from assumptions. Establish the
   reader's task and one primary takeaway before choosing a layout.
2. Create a page and named layers for structure, content, figures, and any
   temporary construction geometry. Use a content-led composition rather than
   treating a favorite card, grid, or colour scheme as a default style.
3. Render early at page and detail scale. Diagnose a concrete reader failure
   (for example, unclear takeaway, unreadable figure label, or hidden fold
   content), make one targeted revision, and render again.
4. Keep a delivery status appropriate to the evidence: a useful concept is not
   automatically print-ready, accessible, or physically proofed.

For active Inkscape work, explicitly select known IDs with `live_select`, make
one narrowly scoped `live_set_style` or transform operation, then use
the returned selected-ID receipt (when available) and `live_render_snapshot`.
The PNG is a document render, not an operating-system screen capture. Do not
pass `expected_revision` to the active-window CLI tools: only an advertised
native bridge can enforce live revision conflicts.

## Safety and conflicts

- Never assume an ID exists: inspect first. A stale `expected_revision` is a
  deliberate conflict failure; re-read the document and decide how to merge.
- Do not use tools omitted from `tools/list`; API-key policy can hide document
  writes, snapshots, or live control.
- Do not use `export_document` or `save_document` with paths outside the
  configured document root.
- Do not use the MCP to close, overwrite, or arbitrarily modify the user's
  active document without explicit authorization.

Read [the RC1 tool catalog](references/rc1-tools.md) when selecting a specific
tool or needing its input conventions.
