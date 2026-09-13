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
| Native bridge | Complete RC1 collaborative live drawing once installed | Optional and ABI-matched; only negotiated methods are advertised |

An absent native bridge is normal. It must never stop offline operation or the
available CLI fallback.

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
   the image bytes. Save or export only after visual acceptance.

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

Only call `live_*` tools after the user has authorized changes to the currently
focused Inkscape document. The active-window compatibility backend targets the
most recently focused window, not a filename selected by the server.

For the CLI backend, use this bounded pattern:

1. `live_status` to inspect advertised Inkscape actions.
2. `live_list_selection` or `live_select` with known SVG IDs.
3. One typed `live_set_style`, `live_move`, `live_rotate`, or `live_scale`.
4. Check the returned selected-ID receipt when `select-list` is advertised,
   then use `live_render_snapshot` to confirm the resulting document pixels.

The snapshot is a rendered document PNG, not an operating-system screenshot.
The CLI backend cannot safely create live primitives, so native-only creation
tools are absent from `tools/list` unless the bridge advertises them. It also
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
