# FreeBSD port-overlay plan

`libmcpinkscape_bridge.so` is a separately installable loaded Inkscape
extension, but it is not a standalone library. It uses private Inkscape C++
headers and `libinkscape_base` ABI, neither of which an installed `inkscape`
package exposes for third-party compilation. A conventional port containing
only `RUN_DEPENDS=inkscape` is therefore invalid.

## Preferred package shape

Build the bridge as an Inkscape port overlay or subpackage from the exact same
source tree and CMake build directory as `graphics/inkscape`.

1. During the Inkscape CMake configure, set
   `CMAKE_PROJECT_TOP_LEVEL_INCLUDES` to
   `mcpInkscape/native/cmake-overlay.cmake` and
   `MCPINKSCAPE_OVERLAY_DIR` to `mcpInkscape/native`.
2. Add `mcpinkscape_bridge` to the port's post-build target list. The target
   links its sibling `inkscape_base`; it must never link an arbitrary installed
   library by filename.
3. Stage the shared library and the three `.inx` descriptors together under
   `${PREFIX}/share/inkscape/extensions/`. Inkscape then discovers the module
   for every user; its runtime UDS is still per-user below
   `~/.local/state/mcpinkscape/`.
4. Package this staged set as an `inkscape-mcpinkscape-bridge` subpackage (or a
   coordinated companion port) with an exact dependency on the matching
   Inkscape package version/ABI. Package upgrades must rebuild both from the
   same port work tree.

The current overlay was compiled and loaded from the local 1.4.4 port work
tree. That is evidence for the integration mechanism, not a claim that its
binary can load into a different installed Inkscape version.

## Port-maintainer acceptance checks

- Build the normal Inkscape port with its ordinary patch set plus the overlay;
  no bridge patch is copied into upstream source.
- Confirm `libmcpinkscape_bridge.so` links to that build's `inkscape_base`.
- With a temporary XDG config directory containing only the staged extension
  files, confirm all three `org.mcpinkscape.bridge.*` actions appear in
  `inkscape --action-list`.
- Run the native bridge verification fixture for a GUI process from the same package.
- Do not install the extension if the package's Inkscape ABI/source revision
  differs from the build inputs.

## Non-goals for RC1

This is not a proposal to install private headers globally, turn the bridge
into a remote service, or replace the optional offline/CLI MCP backends.
Windows packaging is intentionally deferred.
