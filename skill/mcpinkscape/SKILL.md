---
name: mcpinkscape
description: Create, inspect, edit, render, and verify SVG/Inkscape drawings through the mcpInkscape MCP server. Use for offline SVG work or collaborative editing of a local running Inkscape instance; do not use for arbitrary shell execution or non-SVG design tools.
---

# mcpInkscape

Use this skill when a task involves drawing or editing an SVG through the
mcpInkscape MCP server. Prefer its typed tools over raw SVG text or arbitrary
Inkscape actions, so the document remains inspectable and edits are
collaboration-safe.

## Choose a backend

- Use the offline document tools for deterministic document creation and
  editing without a GUI. Work only below the server's configured document root.
- Use `live_*` tools only when `server_status` reports active-window support
  and the user has authorized edits to their running Inkscape document.
- A native bridge, when installed, is optional. Its absence must not block the
  offline or advertised active-window workflow.

## Working method

1. Call `server_status`, then create/open a document and inspect its revision.
2. Use `create_layer`, the typed `create_*` / `create_text` tools, and stable
   object IDs. Use `list_objects`, `get_object`, and `get_object_bounds` to
   verify intermediate state.
3. Apply `set_fill`, `set_stroke`, `set_opacity`, `set_text`, and the transform
   tools (`move_objects`, `rotate_objects`, `scale_objects`) with the latest
   `expected_revision` whenever another editor may be collaborating.
4. Render `render_snapshot`, inspect or retrieve it with
   `get_snapshot_base64`, and save/export only after the result is accepted.

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
