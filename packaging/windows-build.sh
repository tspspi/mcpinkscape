#!/usr/bin/env bash
set -euo pipefail
[[ "$MSYSTEM" == UCRT64 ]] || { echo 'UCRT64 environment required' >&2; exit 1; }
task_root=$(cygpath -u "$MCP_BUILD_ROOT")
task_work=$(cygpath -u "$MCP_BUILD_WORK")
task_prefix=$(cygpath -u "$MCP_BUILD_PREFIX")
case "$MCP_BUILD_ACTION" in
    verify|uninstall)
        exec python "$task_root/packaging/build_native.py" "$MCP_BUILD_ACTION" \
            --work-dir "$task_work" --prefix "$task_prefix" --jobs "$MCP_BUILD_JOBS"
        ;;
esac
mkdir -p "$task_work"
# Keep the supported runtime/toolchain coherent rather than doing a partial
# package database refresh followed by selected dependency upgrades.
pacman -Syu --noconfirm
pacman -S --needed --noconfirm git base-devel mingw-w64-ucrt-x86_64-python
python "$task_root/packaging/build_native.py" fetch --work-dir "$task_work" --prefix "$task_prefix"
task_source=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["inkscape"]["directory"])' "$task_root/packaging/sources.json")
export CI=1 PACMAN_OPTIONS='--needed --noconfirm'
bash -e "$task_work/sources/$task_source/buildtools/msys2installdeps.sh"
python "$task_root/packaging/build_native.py" "$MCP_BUILD_ACTION" \
    --work-dir "$task_work" --prefix "$task_prefix" --jobs "$MCP_BUILD_JOBS"
