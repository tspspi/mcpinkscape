# Inkscape with the native mcpInkscape bridge

Copy this complete directory, including `native/`, into the ports tree as
`graphics/inkscape-mcpinkscape`, then run:

```sh
make -C /usr/ports/graphics/inkscape-mcpinkscape install
```

This is a local slave port of `graphics/inkscape`. It inherits the parent
source archive, patches, options, dependencies, and package list, builds in
its own work directory, and adds the native bridge through the CMake overlay.
You do not edit the parent port, copy libraries manually, or set runtime
resource environment variables. The supported parent source version is 1.4.4;
an unsupported parent version fails explicitly.

The resulting `inkscape-mcpinkscape` package contains both Inkscape and its
matching bridge. It owns the ordinary Inkscape installation paths and conflicts
with the ordinary `inkscape` package. Use the normal ports/pkg replacement
procedure when switching variants; this port does not forcibly remove an
existing package. `pkg delete inkscape-mcpinkscape` removes the combined package.

The Python MCP server is installed and configured separately. Installation
does not start the bridge listener. Start it explicitly from Inkscape's
Extensions menu when needed.

For package maintenance, use the usual `make stage`, `make check-plist`, and
`make package` targets. Test the staged/installed application, including actual
live editing, before distributing a binary. Version equality by itself does
not prove ABI compatibility.

The `native/` payload is generated from the main repository by
`python3 packaging/export_freebsd_port.py`. Its manifest records the source
hashes and base Git revision. Maintainers refresh it when the bridge changes;
end users need no additional source checkout or export command.
