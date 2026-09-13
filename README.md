# Inkscape MCP Server `mcpinkscape`

`mcpinkscape` is an MCP server for creating, inspecting, editing, rendering,
and exporting SVG/Inkscape documents. It provides a deterministic offline SVG
backend and a carefully limited bridge to a locally running Inkscape instance.
MCP clients use typed drawing tools; the server never exposes arbitrary shell
commands, Python execution, raw SVG replacement, or arbitrary Inkscape action
strings.

__WORK IN PROGRESS — RC1 implementation__

Offline SVG drawing, rendering, export, and the version-probed active-window
CLI compatibility bridge are implemented and tested on FreeBSD with Inkscape
1.4.3. The separately buildable native C++ extension provides secure
GUI-thread live editing, snapshots, revision-conflict protection, and polling;
the RC1 native workflow is live-tested against an ABI-matched FreeBSD Inkscape
1.4.4 fixture. Native capabilities remain dynamically advertised because the
extension is optional and its ABI must exactly match Inkscape.

* There is an accompanying [blog article about this project](https://www.tspi.at/2026/09/13/llminkscape.html)
* [Demonstration of the first run on YouTube](https://www.youtube.com/watch?v=i3NMtjDiHq0)

## Features

- offline SVG document lifecycle, layers, object inspection, selection, and
  stable object IDs
- typed rectangles, ellipses, circles, lines, polylines, polygons, paths, and
  text, including absolute SVG units such as `mm`, `cm`, `in`, `pt`, and `px`
- typed fill, stroke/border appearance, opacity, text appearance, page
  background, linear/radial gradients, confined embedded PNG/JPEG placement,
  movement, rotation, scale, grouping, stacking, duplication, and deletion
- Inkscape CLI rendering to registered PNG snapshots plus SVG, plain-SVG, PNG,
  and PDF export
- active-window selection, style, transform, and document rendering through
  only the actions advertised by the installed Inkscape executable
- stdio MCP transport and authenticated Streamable HTTP transport with
  mcpFreeCAD-style API-key policy controls
- optional loaded C++ bridge extension over private UDS on FreeBSD/Linux, or
  literal `127.0.0.1` TCP only on Windows
- repository-owned [coding-agent skill](skill/mcpinkscape/SKILL.md) and RC1
  tool catalog

## Installation

Install from this checkout in an existing Python environment containing the
declared dependencies:

```sh
pip install -e .
```

For remote HTTP mode, install the existing optional dependencies as well:

```sh
pip install -e ".[remote]"
```

Neither command installs an Inkscape extension, creates a configuration file,
or changes user or system configuration.

## Quick Start

1. Copy [examples/mcpinkscape.conf](examples/mcpinkscape.conf) to a location
   you control and set its `stdio.document_root`.
2. Start the stdio MCP server:

   ```sh
   mcpinkscape --config ./mcpinkscape.conf
   ```

3. From an MCP client, call `server_status`, then use `create_document`, the
   typed `create_*` tools, `set_fill`/`set_stroke`, and `render_snapshot`.

The server starts successfully without a GUI or native extension. `server_status`
reports which offline, active-window, and native capabilities are actually
available.

For complete operator guidance and examples, see [USER_MANUAL.md](USER_MANUAL.md).

## Configuration

The configuration layout mirrors mcpFreeCAD. The server does not create or load
the example automatically. Its default configuration path is
`~/.config/mcpinkscape.conf`.

The repository ships two safe starting points:

- Local stdio mode: [examples/mcpinkscape.conf](examples/mcpinkscape.conf)
- Local authenticated HTTP mode:
  [examples/mcpinkscape-remote.conf](examples/mcpinkscape-remote.conf)

Copy an example before changing it. The remote example deliberately binds only
to `127.0.0.1`, has no usable API key yet, and must be initialized explicitly:

```sh
mcpinkscape --config ./mcpinkscape-remote.conf --genkey drawing-agent
```

The command prints the plaintext key once and stores only its derived verifier
in the configuration file. Preserve that key in your chosen secret store; do
not put it in a repository.

The local stdio example has this bridge and document-access structure:

```json
{
  "mode": "stdio",
  "bridge": {
    "mode": "auto",
    "uds": "~/.local/state/mcpinkscape/bridge.sock",
    "tcp_host": "127.0.0.1",
    "tcp_port": 61779,
    "timeout_seconds": 30.0,
    "required": false
  },
  "stdio": {
    "document_root": "./mcpinkscape-library",
    "allow_document_write": true,
    "allow_snapshots": true,
    "allow_live_control": true
  }
}
```

`bridge.mode` is one of `auto`, `native_uds`, `active_window`, or `offline`.
On FreeBSD/Linux a native extension uses the private UDS path. On Windows the
native endpoint is accepted only at literal `127.0.0.1:tcp_port`; its default
port is `61779`. No bridge token is used.

## Connecting with Agents

### Codex example (stdio)

Add this to the Codex configuration, adjusting the configuration path to the
copy you created:

```toml
[mcp_servers.mcpinkscape]
command = "mcpinkscape"
args = [
  "--config", "/path/to/mcpinkscape.conf"
]
startup_timeout_sec = 300
```

### Codex example (remote HTTP)

```toml
[mcp_servers.mcpinkscape]
url = "http://127.0.0.1:61780/mcp/mcp?api_key=<MCPINKSCAPE_API_KEY>"
```

### PQC example (remote HTTP)

Add the following entry beneath `mcp.servers` in the PQC JSON configuration:

```json
"mcpinkscape": {
  "enabled": true,
  "transport": "http",
  "url": "http://127.0.0.1:61780/mcp/mcp",
  "auth": {
    "mode": "query_param",
    "token": "<MCPINKSCAPE_API_KEY>",
    "param_name": "api_key"
  },
  "policy": {
    "enabled": true,
    "visible_by_default": true,
    "approval_mode": "never",
    "network_classification": "local"
  }
}
```

### JSON MCP transport example

```json
{
  "name": "mcpinkscape",
  "type": "mcp",
  "transport": {
    "type": "http",
    "url": "http://127.0.0.1:61780/mcp/mcp?api_key=<MCPINKSCAPE_API_KEY>"
  }
}
```

### FreeBSD service

The server is not installed or enabled automatically. For a deliberate
`remotehttp` deployment, install the repository's
`freebsd/rc.d/mcpinkscape` as `/usr/local/etc/rc.d/mcpinkscape`, place a
reviewed configuration at `/usr/local/etc/mcpinkscape.conf`, then set:

```sh
mcpinkscape_enable="YES"
mcpinkscape_config="/usr/local/etc/mcpinkscape.conf"
```

The script defaults to `remotehttp`, requires both command and configuration,
and supports `mcpinkscape_daemon_user`, `mcpinkscape_transport`, and
`mcpinkscape_flags` overrides. It does not install the native Inkscape bridge;
that remains a separately built, per-user extension.

Remote mode adds `remote_server` and `api_keys` entries. Generate or rotate a
remote API key explicitly with:

```sh
mcpinkscape --config ./mcpinkscape.conf --genkey drawing-agent
```

The plaintext key is printed once; it is not retained in normal status output.

## Running

Run stdio MCP mode:

```sh
mcpinkscape --config ./mcpinkscape.conf
```

Run authenticated Streamable HTTP mode:

```sh
mcpinkscape --config ./mcpinkscape.conf --transport remotehttp
```

The remote wrapper accepts `Authorization: Bearer <key>`, `X-API-Key: <key>`,
`?mcp=<key>`, or `?api_key=<key>`. Its `/status` endpoint is intentionally
public for local health checks; `/mcp` requires a configured API key.

## Native Extension

The `native/` directory is an Inkscape-source-overlay target, not an independent
binary build. It needs the exact matching Inkscape source, build headers,
compiler, and `inkscape_base` ABI. It supplies Start, Stop, and Status entries
under Inkscape’s Extensions menu. Installation is intentionally a separate
explicit action and is not performed by the Python package.

The extension implements bridge lifecycle, structured JSON framing, same-user
transport checks, typed live drawing/style/transform/structure operations,
named undo transactions, revision-conflict protection, page/drawing PNG
snapshot staging, and pollable document/selection changes—including edits made
directly by a human in Inkscape. Tools are advertised only when a connected
bridge implements the matching protocol method.

## Repository Layout

- `src/mcpinkscape/`: Python MCP server, offline SVG backend, transports, and
  native bridge client
- `native/`: C++ loaded-extension source, `.inx` descriptors, and overlay CMake
  target
- `examples/`: configuration example
- `skill/`: OpenAI-compatible coding-agent skill
- `tests/`: Python protocol, service, transport, and real-Inkscape tests
- `USER_MANUAL.md`: operator and MCP-client usage guide

## License

This project is distributed under the BSD-style terms in [LICENSE.md](LICENSE.md).
