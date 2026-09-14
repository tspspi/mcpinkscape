# FreeBSD native application port

The former manual integration fragment has been replaced by a self-contained
local slave port at
[`usr/ports/graphics/inkscape-mcpinkscape`](usr/ports/graphics/inkscape-mcpinkscape/README.md).
Copy that complete directory into a ports tree and run `make install` there.

The port reuses the `graphics/inkscape` recipe while adding the native CMake
overlay. It builds Inkscape and the bridge in the same new work tree and
packages them together. There is no dependency on an existing Inkscape work
directory or on private headers from an installed package. The current source
version admission is 1.4.4.

Native library/descriptors, installed library lookup, license, and native build
metadata are owned by `native/CMakeLists.txt`, shared with the other platform
builds. The port adds the corresponding package-list entries and captures its
parent recipe, patch hashes, source checksum, selected options, and bridge
revision/hashes in `share/inkscape/mcpinkscape/port-inputs.json`. It rejects
recipe or patch changes after configuration and input changes during a build.
Installation leaves the bridge listener off.

The combined package replaces the ordinary Inkscape package through the normal
package-management procedure because both own the same paths. It is not a
standalone plugin promised to work with arbitrary installed Inkscape builds.

## Package maintenance

Use the usual `make stage`, `make check-plist`, and `make package` targets.
See the [installation instructions](../INSTALL.md) for platform support and usage.
