#!/bin/sh
set -eu
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 26.04 ] || [ "$(uname -m)" != x86_64 ]; then
    echo 'Supported Linux build target: Ubuntu 26.04 x86_64.' >&2
    exit 1
fi
# Explicit packages avoid requiring deb-src repositories just to bootstrap.
task_packages='build-essential cmake ninja-build pkg-config ragel git ca-certificates python3 python3-lxml python3-numpy python3-scour python3-appdirs python3-cachecontrol python3-cssselect python3-filelock python3-requests python3-tinycss2 libboost-dev libboost-filesystem-dev libgc-dev libgsl-dev libgtkmm-3.0-dev libgtksourceview-4-dev libxslt1-dev liblcms2-dev libpotrace-dev libpoppler-glib-dev libdouble-conversion-dev lib2geom-dev libgspell-1-dev libenchant-2-dev libepoxy-dev libreadline-dev libpng-dev libjpeg-dev libfontconfig-dev libfreetype-dev libharfbuzz-dev libcdr-dev libvisio-dev libwpg-dev gettext intltool'
if [ "${1:-install}" = package ]; then
    task_packages="$task_packages xvfb dbus libglib2.0-bin libxtst6"
fi
task_missing=''
for task_package in $task_packages; do
    if ! dpkg-query -W -f='${Status}' "$task_package" 2>/dev/null | grep -qx 'install ok installed'; then
        task_missing="$task_missing $task_package"
    fi
done
if [ -n "$task_missing" ]; then
    if [ "$(id -u)" = 0 ]; then
        apt-get update
        apt-get install -y $task_missing
    else
        echo 'Installing missing build prerequisites through sudo.'
        sudo apt-get update
        sudo apt-get install -y $task_missing
    fi
fi
