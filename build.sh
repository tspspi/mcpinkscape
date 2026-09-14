#!/bin/sh
# Single entry point; build details live in packaging/, not user instructions.
set -eu
task_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if [ "$(uname -s)" != Linux ]; then
    echo 'This wrapper supports Linux. On FreeBSD use the supplied port; on Windows use build.ps1.' >&2
    exit 1
fi
case "${1:-install}" in
    -h|--help) exec python3 "$task_root/packaging/build_native.py" --help ;;
    verify|uninstall) exec python3 "$task_root/packaging/build_native.py" "$@" ;;
esac
"$task_root/packaging/linux-dependencies.sh" "${1:-install}"
exec python3 "$task_root/packaging/build_native.py" "$@"
