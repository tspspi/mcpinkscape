#!/usr/bin/env python3
"""Verify an installed application using a disposable GUI and document.

Unix runs in its own D-Bus session and Xvfb display. It never attaches to a
user's existing Inkscape session. Windows execution is not yet implemented.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def undo_with_keyboard(display_name, process_id):
    """Send Ctrl+Z only to a window owned by our process on our private display."""
    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    xtst_name = ctypes.util.find_library("Xtst")
    require(xtst_name, "GUI undo verification requires libXtst")
    xtst = ctypes.CDLL(xtst_name)
    pointer, window = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = pointer
    x11.XDefaultRootWindow.argtypes = [pointer]
    x11.XDefaultRootWindow.restype = window
    x11.XQueryTree.argtypes = [pointer, window, ctypes.POINTER(window), ctypes.POINTER(window),
                              ctypes.POINTER(ctypes.POINTER(window)), ctypes.POINTER(ctypes.c_uint)]
    x11.XInternAtom.argtypes = [pointer, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = window
    x11.XGetWindowProperty.argtypes = [pointer, window, window, ctypes.c_long, ctypes.c_long,
        ctypes.c_int, window, ctypes.POINTER(window), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(pointer)]
    x11.XFree.argtypes = [pointer]
    x11.XRaiseWindow.argtypes = [pointer, window]
    x11.XSetInputFocus.argtypes = [pointer, window, ctypes.c_int, ctypes.c_ulong]
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = window
    x11.XKeysymToKeycode.argtypes = [pointer, window]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XSync.argtypes = [pointer, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [pointer]
    xtst.XTestFakeKeyEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(display_name.encode())
    require(display, "Could not open private test display")
    try:
        root, parent = window(), window()
        children = ctypes.POINTER(window)()
        count = ctypes.c_uint()
        require(x11.XQueryTree(display, x11.XDefaultRootWindow(display), ctypes.byref(root),
                              ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)),
                "Could not enumerate test windows")
        atom = x11.XInternAtom(display, b"_NET_WM_PID", 0)
        target = None
        try:
            for index in range(count.value):
                actual_type, actual_format = window(), ctypes.c_int()
                items, remaining, value = ctypes.c_ulong(), ctypes.c_ulong(), pointer()
                status = x11.XGetWindowProperty(display, children[index], atom, 0, 1, 0, 0,
                    ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(items),
                    ctypes.byref(remaining), ctypes.byref(value))
                if status == 0 and value:
                    if items.value == 1 and actual_format.value == 32 and ctypes.cast(value, ctypes.POINTER(window))[0] == process_id:
                        target = children[index]
                    x11.XFree(value)
        finally:
            x11.XFree(children)
        require(target, "No test window belongs to the launched Inkscape process")
        x11.XRaiseWindow(display, target)
        x11.XSetInputFocus(display, target, 1, 0)
        x11.XSync(display, 0)
        control = x11.XKeysymToKeycode(display, x11.XStringToKeysym(b"Control_L"))
        z = x11.XKeysymToKeycode(display, x11.XStringToKeysym(b"z"))
        for key, pressed in ((control, 1), (z, 1), (z, 0), (control, 0)):
            require(xtst.XTestFakeKeyEvent(display, key, pressed, 0), "Test keyboard injection failed")
        x11.XSync(display, 0)
    finally:
        x11.XCloseDisplay(display)


class Connection:
    def __init__(self, endpoint):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(15)
        try:
            self.socket.connect(str(endpoint))
        except BaseException:
            self.socket.close()
            raise
        self.reader = self.socket.makefile("rb")

    def close(self):
        self.reader.close()
        self.socket.close()

    def call(self, method, params=None, revision=None, error=None):
        request = {"type": "request", "protocol_version": 1, "id": uuid.uuid4().hex,
                   "method": method, "params": params or {}}
        if revision is not None:
            request["expected_revision"] = revision
        self.socket.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(self.reader.readline(1024 * 1024 + 1))
        require(response.get("id") == request["id"], "Bridge response ID mismatch")
        if error:
            require(not response.get("ok") and response.get("error", {}).get("code") == error,
                    f"Expected {error}: {response}")
            return response
        require(response.get("ok"), f"Bridge request failed: {response}")
        return response["result"]


def check(executable, temporary, log):
    env = os.environ.copy()
    for key in ("INKSCAPE_DATADIR", "LD_LIBRARY_PATH", "LD_PRELOAD", "WAYLAND_DISPLAY"):
        env.pop(key, None)
    env.update({"INKSCAPE_PROFILE_DIR": str(temporary / "profile"),
                "XDG_CONFIG_HOME": str(temporary / "config"),
                "XDG_STATE_HOME": str(temporary / "state"),
                "XDG_CACHE_HOME": str(temporary / "cache")})
    endpoint = temporary / "state/mcpinkscape/bridge.sock"
    fixture = temporary / "fixture.svg"
    fixture.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"/>')
    read_fd, write_fd = os.pipe()
    display = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
                               pass_fds=(write_fd,), stdout=log, stderr=log)
    os.close(write_fd)
    application = None
    connection = None
    try:
        import select
        require(select.select([read_fd], [], [], 15)[0], "Xvfb did not provide a display")
        display_number = os.read(read_fd, 64).decode().strip()
        require(display_number.isdigit(), "Xvfb failed to start")
        env["DISPLAY"] = ":" + display_number
        catalog = subprocess.run([executable, "--with-gui", "--batch-process", fixture, "--action-list"],
                                 env=env, capture_output=True, text=True, check=True, timeout=30)
        for action in ("start", "stop", "status"):
            require(f"org.mcpinkscape.bridge.{action}" in catalog.stdout, f"Installed GUI action missing: {action}")
        application = subprocess.Popen([executable, "--with-gui", fixture],
                                        env=env, stdout=log, stderr=log)
        def activate(action):
            subprocess.run(["gdbus", "call", "--session", "--dest", "org.inkscape.Inkscape",
                            "--object-path", "/org/inkscape/Inkscape", "--method", "org.gtk.Actions.Activate",
                            action, "[]", "{}"], env=env, stdout=log, stderr=log, check=True, timeout=20)

        # Launch normally first: installation must not start a listener, and
        # an exported GUI action confirms the application completed startup.
        deadline = time.monotonic() + 45
        ready = False
        while time.monotonic() < deadline:
            require(application.poll() is None, "Installed GUI crashed during normal startup")
            status = subprocess.run(["gdbus", "call", "--session", "--dest", "org.inkscape.Inkscape",
                "--object-path", "/org/inkscape/Inkscape", "--method", "org.gtk.Actions.Describe",
                "org.mcpinkscape.bridge.start"], env=env, stdout=log, stderr=log, timeout=5)
            if status.returncode == 0:
                ready = True
                break
            time.sleep(0.2)
        require(ready, "Installed GUI did not finish startup within 45 seconds")
        time.sleep(2)
        require(application.poll() is None, "Installed GUI crashed after startup")
        require(not endpoint.exists(), "Normal GUI startup unexpectedly enabled the bridge")
        activate("org.mcpinkscape.bridge.start")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(application.poll() is None, "Installed GUI exited before bridge startup")
            try:
                connection = Connection(endpoint)
                break
            except (OSError, ConnectionError):
                time.sleep(0.2)
        require(connection is not None, "Installed bridge did not start within 45 seconds")
        hello = connection.call("bridge.hello")
        require(hello["bridge"] == "mcpinkscape", "Unexpected bridge identity")
        activate("org.mcpinkscape.bridge.status")
        require(connection.call("bridge.status")["running"], "Bridge status does not report running")
        require(endpoint.stat().st_mode & 0o777 == 0o600, "Socket is not private")
        revision = connection.call("document.revision")["revision"]
        created = connection.call("shape.create", {"kind": "rectangle", "object_id": "install-check",
                                  "values": {"x": 4, "y": 4, "width": 32, "height": 24}}, revision)
        require(created["revision"] > revision, "Mutation did not advance the revision")
        connection.call("object.set_style", {"object_ids": ["install-check"], "style": {"fill": "#ff0000"}},
                        revision, error="revision_conflict")
        objects = connection.call("object.list", {"offset": 0, "limit": 100})["objects"]
        require(any(item["id"] == "install-check" for item in objects), "Created object is missing")
        snapshot = connection.call("snapshot.render", {"area": "page"})
        png = Path(snapshot["staging_path"])
        require(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "Snapshot is not a PNG")
        connection.call("snapshot.release", {"snapshot_id": snapshot["snapshot_id"]})
        require(not png.exists(), "Snapshot release did not remove staging file")
        # Undo is a document action, not part of Inkscape's application CLI
        # action group. Exercise the actual UI shortcut on our private display.
        undo_with_keyboard(env["DISPLAY"], application.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            objects = connection.call("object.list", {"offset": 0, "limit": 100})["objects"]
            if not any(item["id"] == "install-check" for item in objects):
                break
            time.sleep(0.1)
        require(not any(item["id"] == "install-check" for item in objects), "Undo did not remove the created object")
        connection.close()
        connection = None
        # Secondary Inkscape command-line processes do not register extension
        # actions before parsing --actions. Activate the running GUI's exported
        # action directly on this test's private session bus instead.
        activate("org.mcpinkscape.bridge.stop")
        deadline = time.monotonic() + 5
        while endpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        require(not endpoint.exists(), "Bridge stop left its socket behind")
        require(application.poll() is None, "Bridge stop unexpectedly terminated the application")
        activate("quit-immediate")
        require(application.wait(timeout=15) == 0, "Installed GUI exited abnormally")
        return {"bridge_version": hello["inkscape_version"], "gui_startup": True, "clean_exit": True,
                "listener_disabled_on_startup": True, "actions": ["start", "stop", "status"], "connection": True,
                "mutation": True, "revision_conflict": True, "undo": True,
                "snapshot": True, "status": True, "stop": True}
    finally:
        os.close(read_fd)
        if connection:
            connection.close()
        for process in (application, display):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inside-session", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("Windows live verification is still pending implementation and a Windows test host")
    require(shutil.which("Xvfb"), "Live verification requires Xvfb")
    require(shutil.which("gdbus"), "Live verification requires gdbus")
    executable = args.executable.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.inside_session:
        require(shutil.which("dbus-run-session"), "Live verification requires dbus-run-session")
        subprocess.run(["dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()),
                        executable, "--output", output, "--inside-session"], check=True)
        return
    with tempfile.TemporaryDirectory(prefix="mcp-live-") as temporary:
        with output.with_suffix(".log").open("w") as log:
            result = check(executable, Path(temporary), log)
    output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
