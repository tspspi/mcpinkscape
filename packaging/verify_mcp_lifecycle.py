#!/usr/bin/env python3
"""Verify MCP-first GUI startup and reconnection in a private Xvfb/D-Bus session.

Requires an ABI-matched Inkscape with the native bridge, Xvfb, and gdbus.
Only disposable documents and processes created here are controlled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def check(executable, temporary, log):
    from fastmcp import Client
    from mcpinkscape.app.server import build_server
    from mcpinkscape.config.schema import AccessConfig, BridgeConfig, Config
    from mcpinkscape.service import NATIVE_TOOL_METHODS

    env = os.environ.copy()
    for key in ("INKSCAPE_DATADIR", "LD_LIBRARY_PATH", "LD_PRELOAD", "WAYLAND_DISPLAY"):
        env.pop(key, None)
    env.update({"INKSCAPE_PROFILE_DIR": str(temporary / "profile"),
                "XDG_CONFIG_HOME": str(temporary / "config"),
                "XDG_STATE_HOME": str(temporary / "state"),
                "XDG_CACHE_HOME": str(temporary / "cache")})
    # Keep CLI subprocesses in the opt-in compatibility test on the same build.
    env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    endpoint = temporary / "state/mcpinkscape/bridge.sock"
    fixture = temporary / "fixture.svg"
    fixture.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">'
                       '<rect id="lifecycle-cli-target" x="5" y="5" width="20" height="20"/></svg>')
    read_fd, write_fd = os.pipe()
    display = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1024x768x24",
                                "-nolisten", "tcp"], pass_fds=(write_fd,), stdout=log, stderr=log)
    os.close(write_fd)
    application = None
    state = None

    def activate(action):
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.inkscape.Inkscape",
                        "--object-path", "/org/inkscape/Inkscape", "--method", "org.gtk.Actions.Activate",
                        action, "[]", "{}"], env=env, stdout=log, stderr=log, check=True, timeout=20)

    async def available(client, expected):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = await client.call_tool("server_status", {})
            if result.data["native_bridge"]["available"] == expected:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"Native bridge availability did not become {expected}")

    def launch():
        process = subprocess.Popen([str(executable), "--with-gui", str(fixture)],
                                   env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                require(process.poll() is None, "GUI exited before readiness")
                try:
                    result = subprocess.run(["gdbus", "call", "--session", "--dest", "org.inkscape.Inkscape",
                        "--object-path", "/org/inkscape/Inkscape", "--method", "org.gtk.Actions.Describe",
                        "org.mcpinkscape.bridge.start"], env=env, stdout=log, stderr=log, timeout=5)
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0:
                    return process
                time.sleep(0.2)
            raise RuntimeError("GUI did not become ready")
        except BaseException:
            process.terminate()
            process.wait(timeout=10)
            raise

    try:
        require(select.select([read_fd], [], [], 15)[0], "Xvfb did not provide a display")
        number = os.read(read_fd, 64).decode().strip()
        require(number.isdigit(), "Invalid Xvfb display")
        env["DISPLAY"] = ":" + number
        server, state = build_server(Config(
            stdio=AccessConfig(document_root=str(temporary / "library")),
            bridge=BridgeConfig(mode="native_uds", required=True, uds=str(endpoint),
                                executable=str(executable), timeout_seconds=5),
        ))
        async with Client(server) as client:
            require(set(NATIVE_TOOL_METHODS) <= {tool.name for tool in await client.list_tools()},
                    "Native tools missing before GUI startup")
            await available(client, False)
            await client.call_tool("create_document", {"name": "offline-before-gui"})
            unavailable = await client.call_tool("live_list_objects", {}, raise_on_error=False)
            require(unavailable.is_error, "Live call unexpectedly succeeded without GUI")
            for cycle in range(2):
                application = launch()
                await available(client, False)
                require(not endpoint.exists(), "GUI startup enabled the bridge unexpectedly")
                activate("org.mcpinkscape.bridge.start")
                await available(client, True)
                await client.call_tool("live_create_shape", {"kind": "rectangle",
                    "values": {"x": 30, "y": 30, "width": 12, "height": 10},
                    "object_id": f"lifecycle-{cycle}", "expected_revision": 0})
                snapshot = await client.call_tool("live_render_snapshot", {"area": "page"})
                require(snapshot.data["backend"] == "native_uds", "Expected native snapshot")
                activate("org.mcpinkscape.bridge.stop")
                await available(client, False)
                activate("org.mcpinkscape.bridge.start")
                await available(client, True)
                objects = await client.call_tool("live_list_objects", {})
                require(any(item["id"] == f"lifecycle-{cycle}" for item in objects.data["objects"]),
                        "Bridge restart lost the live document")
                if cycle == 0:
                    # The bridge serializes clients. Release this idle transport
                    # while the independent live integration clients use it.
                    state.stdio_service.native_bridge.close()
                    test_env = {**env, "MCPINKSCAPE_NATIVE_VERIFY": "1",
                        "MCPINKSCAPE_NATIVE_VERIFY_SOCKET": str(endpoint),
                        "MCPINKSCAPE_LIVE_VERIFY": "1", "MCPINKSCAPE_LIVE_VERIFY_TARGET": "lifecycle-cli-target"}
                    subprocess.run([sys.executable, "-m", "pytest", "-q",
                        "tests/test_live_native_bridge_integration.py", "tests/test_live_active_window_integration.py"],
                        cwd=Path(__file__).resolve().parents[1], env=test_env,
                        stdout=log, stderr=log, check=True, timeout=180)
                # Discard is appropriate only for this private disposable fixture.
                activate("quit-immediate")
                require(application.wait(timeout=15) == 0, "GUI exited abnormally")
                application = None
                await available(client, False)
        return {"mcp_before_gui": True, "offline_before_gui": True, "late_bridge_connection": True,
                "bridge_restart": True, "gui_relaunch_same_mcp_session": True,
                "native_integration": True, "active_window_integration": True, "clean_gui_exit": True}
    finally:
        if state is not None:
            state.close()
        os.close(read_fd)
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
    require(os.name != "nt", "This verifier requires Unix Xvfb and D-Bus")
    for command in ("Xvfb", "gdbus", "dbus-run-session"):
        require(shutil.which(command), f"Missing test prerequisite: {command}")
    executable, output = args.executable.resolve(), args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.inside_session:
        subprocess.run(["dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()),
                        str(executable), "--output", str(output), "--inside-session"], check=True)
        return
    with tempfile.TemporaryDirectory(prefix="mcp-lifecycle-") as temporary:
        with output.with_suffix(".log").open("w") as log:
            result = asyncio.run(check(executable, Path(temporary), log))
    output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
