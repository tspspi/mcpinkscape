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
import socket
import subprocess
import sys
import tempfile
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def check(executable, temporary, log, layout_only=False):
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

    def activate(action, application_id="org.inkscape.Inkscape", parameters="[]"):
        subprocess.run(["gdbus", "call", "--session", "--dest", application_id,
                        "--object-path", "/" + application_id.replace(".", "/"), "--method", "org.gtk.Actions.Activate",
                        action, parameters, "{}"], env=env, stdout=log, stderr=log, check=True, timeout=20)

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
        os.environ.update(env)
        configuration = Config(
            stdio=AccessConfig(document_root=str(temporary / "library")),
            bridge=BridgeConfig(mode="native_uds", required=True, uds=str(endpoint),
                                executable=str(executable), timeout_seconds=5),
        )
        server, state = build_server(configuration)
        # Fresh stdio servers may inherit no graphical environment. Verify an
        # initial launch using the explicitly configured desktop environment.
        initial = configuration.model_copy(deep=True)
        initial.bridge.desktop_environment = {key: env[key] for key in (
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR") if key in env}
        initial_path = temporary / "initial.conf"
        initial_path.write_text(initial.model_dump_json())
        source = temporary / "library/startup-existing.svg"
        source.write_text(fixture.read_text())
        original_source = source.read_bytes()
        initial_worker = r"""
import asyncio, json, sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
async def run():
    async with Client(StdioTransport(sys.executable, ["-m", "mcpinkscape", "--config", sys.argv[1]], keep_alive=False)) as client:
        started = (await client.call_tool("inkscape_start", {"path": "startup-existing.svg"})).data
        assert started["started"] and started["owned"]
        await client.call_tool("live_create_shape", {"kind": "circle", "values": {"cx": 40, "cy": 40, "r": 2},
            "object_id": "fresh-stdio-start", "expected_revision": 0})
        stopped = (await client.call_tool("inkscape_shutdown", {})).data
        assert stopped["stopped"] and stopped["saved_paths"]
        print(json.dumps(stopped))
asyncio.run(run())
"""
        initial_run = subprocess.run([sys.executable, "-c", initial_worker, str(initial_path)],
            env=env, capture_output=True, text=True, timeout=90)
        log.write(initial_run.stderr)
        log.flush()
        require(initial_run.returncode == 0, "Fresh stdio launch failed: " + initial_run.stderr)
        initial_saved = json.loads(initial_run.stdout)["saved_paths"]
        require(source.read_bytes() == original_source, "Shutdown overwrote the input SVG")
        require(any("fresh-stdio-start" in (temporary / "library" / path).read_text() for path in initial_saved),
                "Fresh stdio edit was not saved")
        async with Client(server) as client:
            require(set(NATIVE_TOOL_METHODS) <= {tool.name for tool in await client.list_tools()},
                    "Native tools missing before GUI startup")
            await available(client, False)
            await client.call_tool("create_document", {"name": "offline-before-gui"})
            unavailable = await client.call_tool("live_list_objects", {}, raise_on_error=False)
            require(unavailable.is_error, "Live call unexpectedly succeeded without GUI")
            if layout_only:
                from verify_layout import verify_layout
                return {"layout_only": True, "layout": await verify_layout(client, state, env, activate)}
            from verify_batches import verify_batches
            batch_checks = await verify_batches(client, state, env, activate)
            from verify_layout import verify_layout
            layout_checks = await verify_layout(client, state, env, activate)
            from verify_live_inspection import verify_live_inspection
            inspection_checks = await verify_live_inspection(client, state)
            from verify_document_targets import verify_document_targets
            target_checks = await verify_document_targets(client, state, configuration, env, activate, temporary, log)
            started = (await client.call_tool("inkscape_start", {})).data
            require(started["started"] and started["owned"], "Managed startup did not establish ownership")
            original_pid = started["pid"]
            again = (await client.call_tool("inkscape_start", {})).data
            require(not again["started"] and again["pid"] == original_pid, "Start launched a duplicate GUI")
            created = await client.call_tool("live_create_shape", {"kind": "rectangle",
                "values": {"x": 30, "y": 30, "width": 12, "height": 10},
                "object_id": "lifecycle-managed", "expected_revision": 0})
            snapshot = await client.call_tool("live_render_snapshot", {"area": "page"})
            require(snapshot.data["backend"] == "native_uds", "Expected native snapshot")
            record = state.stdio_service.application._record()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
                idle.connect(str(endpoint))
                activate("org.mcpinkscape.bridge.stop", record["application_id"])
            # No Start action or intermediate status call: a live edit must
            # enable and reconnect the stopped bridge automatically.
            await client.call_tool("live_create_shape", {"kind": "circle",
                "values": {"cx": 70, "cy": 70, "r": 5}, "object_id": "after-bridge-restart",
                "expected_revision": created.data["revision"]})
            refused = await client.call_tool("inkscape_shutdown", {"save_mode": "refuse"}, raise_on_error=False)
            require(refused.is_error, "Shutdown did not protect unsaved documents")
            require((await client.call_tool("inkscape_status", {})).data["running"], "Refused shutdown closed GUI")

            # Recreate MCP in a genuinely new process over stdio. Its persisted
            # ownership must allow resuming edits and saving/closing this GUI.
            config_path = temporary / "restart.conf"
            config_path.write_text(configuration.model_dump_json())
            worker = r"""
import asyncio, json, sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
async def run():
    transport = StdioTransport(sys.executable, ["-m", "mcpinkscape", "--config", sys.argv[1]], keep_alive=False)
    async with Client(transport) as client:
        status = (await client.call_tool("inkscape_status", {})).data
        assert status["owned"]
        current = (await client.call_tool("live_poll_changes", {"after_revision": 0, "after_selection_generation": 0})).data
        await client.call_tool("live_create_shape", {"kind": "circle", "values": {"cx": 20, "cy": 20, "r": 4},
            "object_id": "after-mcp-restart", "expected_revision": current["revision"]})
        stopped = (await client.call_tool("inkscape_shutdown", {})).data
        assert stopped["stopped"] and stopped["saved_paths"]
        print(json.dumps(stopped))
asyncio.run(run())
"""
            resumed = subprocess.run([sys.executable, "-c", worker, str(config_path)], env=env,
                capture_output=True, text=True, check=False, timeout=90)
            log.write(resumed.stderr)
            log.flush()
            require(resumed.returncode == 0, "Restarted stdio MCP failed: " + resumed.stderr)
            saved = json.loads(resumed.stdout)["saved_paths"]
            for path in saved:
                contents = (temporary / "library" / path).read_text()
                require("after-mcp-restart" in contents and "after-bridge-restart" in contents,
                        "Saved document did not preserve completed edits")
            state.stdio_service.application._process.wait(timeout=10)
            await available(client, False)

            # Crash/relaunch and explicit discard use only our disposable GUI.
            restarted = (await client.call_tool("inkscape_start", {})).data
            require(restarted["pid"] != original_pid, "Expected a fresh GUI process")
            process = state.stdio_service.application._process
            process.kill()
            process.wait(timeout=10)
            unavailable = await client.call_tool("live_list_objects", {}, raise_on_error=False)
            require(unavailable.is_error, "Live operation unexpectedly succeeded after GUI crash")
            await client.call_tool("inkscape_start", {})
            await client.call_tool("live_create_shape", {"kind": "circle", "values": {"cx": 8, "cy": 8, "r": 3},
                "object_id": "discard-fixture", "expected_revision": 0})
            discarded = (await client.call_tool("inkscape_shutdown", {"save_mode": "discard"})).data
            require(discarded["stopped"] and not discarded["saved_paths"], "Explicit discard did not complete")

            await client.call_tool("inkscape_start", {})
            handoff = state.stdio_service.application._record()
            require((await client.call_tool("inkscape_release", {})).data["released"], "Handoff failed")
            denied = await client.call_tool("inkscape_shutdown", {}, raise_on_error=False)
            require(denied.is_error, "Released application was still owned")
            activate("org.mcpinkscape.bridge.stop", handoff["application_id"])
            require((await client.call_tool("live_list_objects", {})).data["open"], "Released GUI did not reconnect")
            activate("quit-immediate", handoff["application_id"])
            state.stdio_service.application._process.wait(timeout=10)

        # GUI-first ordering: create an ordinary user-style instance, then
        # construct a new MCP server. Neither startup requires a manual action.
        state.close()
        application = launch()
        server, state = build_server(configuration)
        async with Client(server) as client:
            require((await client.call_tool("live_list_objects", {})).data["open"], "GUI-first connection failed")
            ordinary = (await client.call_tool("live_list_documents", {})).data["documents"]
            require(len(ordinary) == 1, "Expected one ordinary fixture document")
            ordinary_target = {key: ordinary[0][key] for key in ("instance_id", "document_id")}
            objects = (await client.call_tool("live_list_objects", ordinary_target)).data["objects"]
            require(any(item["id"] == "lifecycle-cli-target" for item in objects), "Cannot target a user-style application")
            target_checks["user_launched_application"] = True
            require(not (await client.call_tool("inkscape_status", {})).data["owned"], "Attached GUI became owned")
            denied = await client.call_tool("inkscape_shutdown", {}, raise_on_error=False)
            require(denied.is_error, "Unmanaged application shutdown was allowed")
            test_env = {**env, "MCPINKSCAPE_NATIVE_VERIFY": "1",
                "MCPINKSCAPE_NATIVE_VERIFY_SOCKET": str(endpoint), "MCPINKSCAPE_LIVE_VERIFY": "1",
                "MCPINKSCAPE_LIVE_VERIFY_TARGET": "lifecycle-cli-target"}
            subprocess.run([sys.executable, "-m", "pytest", "-q",
                "tests/test_live_native_bridge_integration.py", "tests/test_live_active_window_integration.py"],
                cwd=Path(__file__).resolve().parents[1], env=test_env,
                stdout=log, stderr=log, check=True, timeout=180)
            activate("quit-immediate")
            require(application.wait(timeout=15) == 0, "Private GUI did not exit normally")
            application = launch()
            require((await client.call_tool("live_list_objects", {})).data["open"], "Unmanaged GUI relaunch failed")
            activate("quit-immediate")
            require(application.wait(timeout=15) == 0, "Relaunched GUI did not exit normally")
            application = None
        return {"layout": layout_checks, "atomic_batches": batch_checks, "live_inspection": inspection_checks, "document_targeting": target_checks, "mcp_before_gui": True, "offline_before_gui": True, "late_bridge_connection": True,
                "automatic_startup": True, "stdio_fresh_start": True, "startup_document": True,
                "automatic_bridge_activation": True, "gui_before_mcp": True,
                "bridge_restart": True, "mcp_process_restart": True, "gui_crash_recovery": True,
                "gui_relaunch_same_mcp_session": True, "save_on_shutdown": True,
                "refuse_unsaved_shutdown": True, "explicit_discard": True, "interactive_handoff": True,
                "unmanaged_shutdown_refused": True, "native_integration": True,
                "active_window_integration": True, "clean_gui_exit": True}
    finally:
        if state is not None:
            manager = state.stdio_service.application
            record = manager._record()
            if record and manager._process_alive(record):
                # This record belongs to this private disposable fixture, even
                # when the process was launched by the stdio child server.
                try:
                    activate("quit-immediate", record["application_id"])
                except subprocess.SubprocessError:
                    pass
            managed = state.stdio_service.application._process
            if managed and managed.poll() is None:
                managed.terminate()
                try:
                    managed.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    managed.kill()
                    managed.wait()
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
    parser.add_argument("--layout-only", action="store_true", help="Run only the isolated native layout checks")
    parser.add_argument("--inside-session", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(os.name != "nt", "This verifier requires Unix Xvfb and D-Bus")
    for command in ("Xvfb", "gdbus", "dbus-run-session"):
        require(shutil.which(command), f"Missing test prerequisite: {command}")
    executable, output = args.executable.resolve(), args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.inside_session:
        subprocess.run(["dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()),
                        str(executable), "--output", str(output), "--inside-session"] +
                       (["--layout-only"] if args.layout_only else []), check=True)
        return
    with tempfile.TemporaryDirectory(prefix="mcp-lifecycle-") as temporary:
        with output.with_suffix(".log").open("w") as log:
            result = asyncio.run(check(executable, Path(temporary), log, layout_only=args.layout_only))
    output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
