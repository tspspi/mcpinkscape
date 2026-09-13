# `inkscape-mcpinkscape` ports overlay

This directory is intended to be copied beneath a FreeBSD ports tree as
`graphics/inkscape-mcpinkscape`. It describes the native bridge portion of the
`mcpinkscape` distribution. The Python MCP server itself is packaged from its
own Python distribution; the bridge must be built with the matching
`graphics/inkscape` work tree.

It is intentionally not a standalone port. An installed `inkscape` package
does not provide the private headers or a supported third-party ABI for
`libinkscape_base`, so a companion port that merely declares `RUN_DEPENDS` on
Inkscape would produce an unsafe binary.

## Integration procedure

1. Copy this directory into the ports tree next to `graphics/inkscape`.
2. In the matching Inkscape port Makefile, include
   `../inkscape-mcpinkscape/files/inkscape-mcpinkscape.mk` before
   `.include <bsd.port.mk>`.
3. Set `MCPINKSCAPE_OVERLAY_DIR` to the `native/` directory from the exact
   `mcpinkscape` release source used for the Python package build.
4. Add the entries from `pkg-plist.bridge` to the Inkscape package plist (or
   to its coordinated bridge subpackage plist).
5. Build and package Inkscape and the bridge from the same patched work tree.

The overlay configures the additional CMake target, builds it after the normal
Inkscape build, and stages the shared library with the three extension
descriptors. It does not enable the per-user bridge listener: the user starts
that explicitly from Inkscape.

The staged bridge is valid only for the exact Inkscape source revision, port
patch set, compiler configuration, and `inkscape_base` ABI used for its build.
Rebuild it whenever the Inkscape package changes.
