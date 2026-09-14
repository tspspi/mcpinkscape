# Install the native application

The native build entry points produce Inkscape and the mcpInkscape bridge as one matched application. You do not need to assemble an Inkscape build, copy extension files, or set resource/library environment variables.

The Python MCP server remains a separate installation; see [README.md](README.md) for its configuration. Installing the native application does not start a bridge listener. Use **Start MCP Inkscape Bridge** in the Extensions menu when you want live editing.

FreeBSD and Ubuntu builds have been tested with the GUI and native bridge. FreeBSD installation on a clean system has not yet been tested. Windows support is experimental and untested.

## FreeBSD

Place the complete `freebsd/usr/ports/graphics/inkscape-mcpinkscape` directory at `graphics/inkscape-mcpinkscape` in your ports tree, then run:

```sh
make -C /usr/ports/graphics/inkscape-mcpinkscape install
```

The local slave port reuses `graphics/inkscape` and builds the editor and bridge together in its own work directory. The currently supported parent source release is **1.4.4**. Ports handles dependencies and the usual installation privilege prompt.

This package owns the normal Inkscape installation paths and conflicts with the ordinary `inkscape` package. It does not forcibly remove an existing installation; use the normal ports/pkg replacement procedure when switching variants. Remove the combined package with `pkg delete inkscape-mcpinkscape`. Do not mix the bridge from this package with another Inkscape build.

Maintainers can use the normal `make stage`, `make check-plist`, and `make package` targets. The port payload is self-contained: end users do not need this repository after copying the directory.

## Linux

Initial target: **Ubuntu 26.04 x86_64**.

```sh
./build.sh install
```

The wrapper installs missing distribution prerequisites using apt and, when necessary, sudo. It downloads and verifies the pinned Inkscape source archive, builds incrementally, checks the staged application, and installs it under `~/.local/opt/mcpinkscape`.

Launch **Inkscape (mcpInkscape)** from the desktop menu, or run `~/.local/opt/mcpinkscape/bin/inkscape`. The dedicated prefix keeps it separate from the distribution's Inkscape.

Optional build controls:

```sh
./build.sh install --jobs 4 --work-dir /path/to/build-cache --prefix /path/to/application
```

The work directory and installation prefix must be separate. Keep the work directory for incremental rebuilds. Installation refuses to overwrite an unrelated directory. An upgrade retains the last application at `<prefix>.previous`, replacing only a previously managed backup. Run `./build.sh uninstall` (with the same `--prefix` if customized) to remove the managed application, backup, and matching desktop entry. The build cache is retained.

## Windows

Initial target: **Windows x64**, using MSYS2 UCRT64.

```powershell
.\build.ps1 install
```

The wrapper bootstraps a checksum-verified MSYS2 installer into a dedicated location, obtains the selected Inkscape release's dependencies, and invokes the shared build driver. The default application prefix is `%LOCALAPPDATA%\Programs\mcpinkscape`. Build options include `-Jobs`, `-WorkDir`, `-Prefix`, and `-MsysRoot`.

Use `.\build.ps1 uninstall` with the same custom prefix/toolchain options, if any, to remove the managed application and its previous version. Verification and removal reuse the existing toolchain without updating or installing packages. Removal retains the toolchain and build cache.

**Windows support is experimental and untested.** The `package` action is currently unavailable on Windows.

## Package builds and verification

The `build`, `install`, and `package` actions use the same driver, native CMake overlay, and staging rules. `verify` checks the existing stage without rebuilding it.

```sh
./build.sh package
```

The command runs rendering and bridge tests in a private Xvfb display and D-Bus session, then creates application and source archives with SHA256 checksums. The Linux wrapper installs the required test packages (`xvfb`, `dbus`, `libglib2.0-bin`, and `libxtst6`).

The Linux archive requires the supported distribution's runtime libraries.

The staged application includes native source hashes and toolchain information. The shared driver additionally records the selected source archive, configuration flags, installed dependency versions, and verification results in `mcpinkscape-build.json`.

FreeBSD packages also include `share/inkscape/mcpinkscape/port-inputs.json`, recording the parent port recipe and patch hashes, source checksum, selected options, and bridge revision/hashes. Changes to the configured parent recipe require a clean port rebuild.

The source archive uses the native input snapshot actually compiled. Editing the checkout during a long build therefore does not change the accompanying native sources or the recorded hashes for that build.

## Maintaining recipes

- `packaging/sources.json` pins downloaded Inkscape and MSYS2 inputs.
- `native/CMakeLists.txt` owns native installation paths, library lookup, and source-version admission.
- `packaging/build_native.py` owns fetch, configure, build, stage, validation, installation, and archive production.
- `packaging/verify_live.py` exercises the staged application independently.
- After changing native sources, run `python3 packaging/export_freebsd_port.py` to refresh the distributed port payload. The packaging tests reject stale payloads.

An Inkscape update requires rebuilding and testing the bridge. Updating a version string alone is not a compatibility check.
