# mcpInkscape User Manual

## Purpose

Use `mcpinkscape` to give an MCP client a constrained drawing surface for SVG
documents and, when available, a local Inkscape window. It is intended for
collaboration: the user can edit an SVG normally while an agent performs typed,
inspectable operations and returns a rendered PNG for visual feedback.

The server is not a general remote-control channel. It does not run arbitrary
Inkscape actions, raw XML, shell commands, or Python submitted by an MCP client.

## Select the Backend

Call `server_status` first.

| Backend | Use it for | Limits |
| --- | --- | --- |
| Offline SVG | Deterministic drawing, headless workflows, exports | Changes only documents under `document_root` |
| Active-window CLI | A focused local Inkscape document | Installed action list limits it to selection, supported styles/transforms, rendered feedback, and no revision-conflict protection |
| Native bridge | Complete RC1 collaborative live drawing once installed | Optional and ABI-matched; configured tools remain discoverable before connection, then capabilities follow negotiated methods |

An absent native bridge is normal. The MCP server starts and connects on the
next status check or live call after Inkscape and the bridge become available.
Offline operations remain usable. `bridge.required: true` requires the native
backend for live calls and disables the CLI fallback; it does not prevent startup.

## Offline Drawing Workflow

1. Use `create_document` or `open_document` and retain its `document_id` and
   `revision`.
2. Create a layer if desired with `create_layer`.
3. Use `create_rectangle`, `create_ellipse`, `create_circle`, `create_line`,
   `create_polyline`, `create_polygon`, `create_path`, or `create_text`.
4. Inspect results with `list_objects`, `get_object`, and `get_object_bounds`.
5. Apply appearance with `set_fill`, `set_stroke`, `set_opacity`, `set_style`,
   `create_gradient` / `apply_gradient`, and `set_page_background`. Basic
   stroke control includes dash pattern, cap, join, and miter limit.
6. Use `import_image` for a PNG/JPEG already below `document_root`. The server
   embeds the verified image by default; it never exposes arbitrary file reads.
7. Apply `move_objects`, `rotate_objects`, or `scale_objects`.
8. Call `render_snapshot`; use `get_snapshot_base64` for a client that needs
   the image bytes. Check the rendered result before saving or exporting.

Use the revision returned by each mutation as `expected_revision` for the next
edit when another editor or agent may change the same document. A revision
mismatch is a conflict: inspect the new state before deciding whether to retry.

Coordinates are SVG user units. Numeric values mean `px`; absolute SVG units
such as `"10mm"`, `"2cm"`, `"1in"`, `"12pt"`, and `"48px"` are accepted.

## Appearance Terms

- foreground means an object’s `fill`
- outline means an object’s `stroke`
- background means the Inkscape page/named-view background
- opacity values are decimal fractions from `0` through `1`

Use CSS-style colour values accepted by the typed tools, for example `#1565c0`,
`#123`, `rgb(20, 90, 180)`, or `none`.

## Running Inkscape Collaboration

For an agent-managed GUI, use:

1. `inkscape_start`, optionally with an existing SVG `path` relative to
   `document_root`. It launches the configured `bridge.executable`, opens a
   document without a splash dialog, enables the bridge, and waits for readiness.
   Calling it again attaches to the running application without creating a duplicate.
2. Use `live_*` tools with revision checks and rendered feedback.
3. `inkscape_shutdown` saves modified documents as new SVG copies under
   `document_root/inkscape-sessions/` and returns their relative `saved_paths`.
   Existing source files are preserved. Linked asset references are rebased.
   Use `save_mode: "refuse"` to keep modified documents open, or explicitly
   choose `save_mode: "discard"` to close without saving.
4. For interactive handoff, use `inkscape_release`. The application stays open
   and reconnectable, and the server relinquishes permission to shut it down.

Application control requires a Unix graphical session, session D-Bus, `gdbus`
in PATH, and the updated ABI-matched native extension. Windows application
lifecycle control is unavailable. Local stdio enables `allow_application_control`
by default; remote API keys default to false and must opt in explicitly.
The permission also requires `allow_live_control`. Saving on shutdown requires
`allow_document_write`.

The server inherits its desktop environment. If your MCP client strips graphical
environment variables, set `bridge.desktop_environment` to the desktop session's
`DISPLAY` (or `WAYLAND_DISPLAY`), `DBUS_SESSION_BUS_ADDRESS`, and, where needed,
`XAUTHORITY` and `XDG_RUNTIME_DIR`. Managed session state retains these values so
a restarted server can control the existing application even with a stripped
environment. Current inherited values take precedence for a new launch.

The server can start before or after Inkscape. With `bridge.auto_enable: true`
(the default), native status checks and live calls activate an available bridge
without requiring the user to select an extension action. They reconnect after
bridge or GUI restarts. MCP disconnection leaves the GUI open, and a new MCP
process can recover ownership of its managed session from private local state.
The server checks session identity, PID, document root, and access-policy owner
before closing an application. Attaching to a user-launched application does
not grant shutdown ownership. Managed applications use the native backend;
CLI fallback never redirects their operations into a separate user window.

`bridge.startup_timeout_seconds` defaults to 45 and
`bridge.shutdown_timeout_seconds` to 20. A readiness failure reports the managed
application log path and retains session state for recovery. The server does
not forcibly terminate applications on startup or shutdown timeout.

Only edit documents covered by the user's authorization. With the native bridge,
call `live_list_documents` and retain the chosen document's `instance_id` and
`document_id`. Pass both values to every subsequent live inspection, selection,
edit, revision poll, and snapshot. These calls operate on that document even
when the user focuses another window. A document without a window is listed
with `desktop_available: false` and cannot receive live operations.

The IDs survive bridge reconnects and MCP process restarts while the document
remains open. Closing and reopening the same file creates a new document ID;
restarting Inkscape creates a new instance ID. A stale target fails explicitly.
Enumerate again and confirm the intended document before continuing. Targeted
requests require a bridge advertising `document_targeting` and never fall back
to the active-window CLI. Successful receipts and snapshots include both IDs.

Omitting both IDs preserves focus-based compatibility behavior. The active-window
CLI backend targets the most recently focused window and cannot accept explicit
document targets. Live document IDs are session handles, separate from offline
document IDs and filenames.

For native inspection, use `live_get_document_info` for page dimensions, root
attributes, the current destination layer, and coordinate metadata.
`live_get_object` returns an object's attributes, parent and child IDs, inherited
paint and basic typography, local transform, transform into document space, and
geometric/visual bounds. Text also includes its content and laid-out glyph bounds.
Non-rendering objects have null bounds; visual bounds include stroke, clipping,
masking, and filter extents as calculated by Inkscape. Inspection larger than
512 KiB fails explicitly; inspect individual descendants of large groups.

Bounds use document pixels with a top-left origin, x increasing rightward and y
downward; one CSS inch is 96 pixels. Affine matrices are `[a,b,c,d,e,f]`, mapping
`(x,y)` to `(a*x+c*y+e, b*x+d*y+f)`. `root_to_document` includes the viewBox mapping.
Drawing coordinates and the effective style's `stroke_width_local` and
`font_size_local` use the destination/object's local SVG user space. Use the
returned matrices to compare them with document-pixel bounds; do not assume that
one user unit equals one pixel. Cumulative opacity includes ancestor opacity;
the reported paint and typography fields are a supported subset of CSS.

`live_list_layers` returns paginated nested layers with parent layer IDs, labels,
and each layer's own hidden/locked flags. `live_create_layer` creates a layer at
the root, or inside `parent_layer_id`. Use `layer_id` on shape/text creation for
an explicit destination, or `live_set_current_layer` to choose the default layer
for subsequent drawing, including image imports. Both layer creation and setting
the destination require `expected_revision`. Setting the destination preserves
the selection and does not create an SVG edit or undo entry. Inspect and render
the result with the same document target.

For an edit sequence that must succeed together, use native `live_apply_batch`.
Supply both document target IDs, the current `expected_revision`, and 1–100 typed
`operations` within a 256 KiB request. Each entry has a `method` and typed `params`.
For example:

```json
[
  {"method": "shape.create", "params": {"kind": "rectangle", "object_id": "panel", "values": {"x": 10, "y": 10, "width": 40, "height": 20}}},
  {"method": "object.set_style", "params": {"object_ids": ["panel"], "style": {"fill": "#336699"}}},
  {"method": "object.move", "params": {"object_ids": ["panel"], "dx": 5, "dy": 0}}
]
```

Creation needs explicit IDs so later operations can refer to them. The whole
sequence is validated before the live edit begins. A successful batch returns one
new revision, ordered operation receipts, and affected stable IDs; it takes one
undo step when `changed` is true. A no-op reports `changed: false` and preserves
revision and undo/redo history. Surviving selections and the current destination layer are preserved.
A validation error leaves the document untouched. A reported execution error rolls
back changes, selection, modified state, and revision without clearing existing
redo history. If the batch deletes the destination layer, the root becomes the
new destination.

Batches support native shape/text/layer creation, text/style changes, transforms,
delete/group/ungroup/stacking, gradients, and page background. Image import,
duplication, selection, and choosing the destination layer remain separate tools.
See [the batch protocol](native/ATOMIC_BATCHES.md) for the supported method names.
Never replay a batch after a timeout or lost response: inspect the same target
and revision first, because it may already have committed.

For the CLI backend, use this bounded pattern:

1. `live_status` to inspect advertised Inkscape actions.
2. `live_list_selection` or `live_select` with known SVG IDs.
3. One typed `live_set_style`, `live_move`, `live_rotate`, or `live_scale`.
4. Check the returned selected-ID receipt when `select-list` is advertised,
   then use `live_render_snapshot` to confirm the resulting document pixels.

The snapshot is a rendered document PNG, not an operating-system screenshot.
The CLI backend cannot safely create live primitives, so native-only creation
tools are hidden in explicit `active_window` or `offline` mode. In `auto` and
`native_uds` mode they remain discoverable while waiting for the native bridge;
calls report backend unavailability until it is enabled. It also
cannot observe a live document revision, so live CLI mutations reject
`expected_revision` rather than silently claiming conflict protection.

With the native bridge, call `live_poll_changes` before a consequential edit,
using the last observed `revision` and `selection_generation`. A changed
revision means a person or another controller edited the document; inspect it
before issuing a new mutation. A changed selection generation is useful UI
state feedback but does not by itself invalidate the document revision.

The repository test suite contains an opt-in live verification harness. Set both
`MCPINKSCAPE_LIVE_VERIFY=1` and `MCPINKSCAPE_LIVE_VERIFY_TARGET` to the known SVG
ID in a dedicated, user-authorized open fixture before running it. It is skipped
otherwise and never chooses an active document or object on its own.

For automated Unix GUI verification, install the development dependencies
(`pip install -e '.[dev]'`) and provide Xvfb, gdbus, dbus-run-session, and a matched
native Inkscape build. Run
`MCPINKSCAPE_GUI_VERIFY_EXECUTABLE=/path/to/inkscape pytest -q tests/test_gui_lifecycle_integration.py`.
This fixture creates its own display, session bus, profile, and disposable
documents. It checks lifecycle/reconnect behavior, explicit targets, inspection,
layers, and rendered pixels without attaching to a working desktop session.
The complete repository suite also exercises remote HTTP; install
`pip install -e '.[dev,remote]'` before running `pytest -q` for that suite.

## Native Bridge and Platform Security

FreeBSD/Linux use a bridge UDS at
`~/.local/state/mcpinkscape/bridge.sock` by default. The server verifies a
socket path, current-user ownership, and non-group/non-world permissions before
connecting. There is no local bridge token.

## FreeBSD rc.d operation

The repository includes `freebsd/rc.d/mcpinkscape` for supervised remote HTTP
operation. It is intentionally not installed or enabled by package setup.
After reviewing the config, install it as `/usr/local/etc/rc.d/mcpinkscape`
and add these explicit rc.conf values:

```sh
mcpinkscape_enable="YES"
mcpinkscape_config="/usr/local/etc/mcpinkscape.conf"
```

Use `mcpinkscape_daemon_user` to drop privileges, and override
`mcpinkscape_transport` only when the configuration supports that transport.
The service starts the MCP server; it does not start Inkscape or load the
optional per-user bridge extension.

Windows uses TCP only at literal `127.0.0.1` on the configured `tcp_port`
(default `61779`). It must never use a wildcard, hostname, IPv6 loopback, or
LAN address. Windows loopback does not provide the same peer-credential
property as Unix UDS, so it should remain local to the user’s desktop session.

The extension is separately built and installed against the exact matching
Inkscape build. Starting it from Inkscape is explicit; merely placing the files
in an extension directory does not start a listener.

## Remote HTTP Mode

Remote HTTP mode is optional. Each API key has a separate document root and
may disable document writes, snapshots, or live control. Disabled capabilities
are omitted from `tools/list` and denied if called directly.

Use a local reverse proxy or an explicitly configured local listener as
appropriate for the deployment. Do not expose the native bridge itself over a
network; it is an internal MCP-server-to-Inkscape transport.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No live tools work | Call `server_status`; verify Inkscape is running and `bridge.mode` permits the backend |
| A live action is unsupported | Compare `live_status.actions` to the requested operation; installed versions differ |
| Native-only tools are missing | The extension is absent, stopped, incompatible, or does not advertise that method |
| Revision conflict | Re-read the offline document or native live objects; do not blindly replay a mutation |
| Snapshot fails | Confirm `allow_snapshots`, Inkscape CLI availability, and the selected render area |
| Path is denied | Use a relative path below the configured `document_root` |

For coding-agent usage and the authoritative RC1 tool family catalog, read
[skill/mcpinkscape/SKILL.md](skill/mcpinkscape/SKILL.md).
