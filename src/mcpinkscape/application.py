"""Managed GUI sessions, automatic bridge activation, and restart-safe ownership."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from mcpinkscape.errors import InvalidDrawingValueError, LiveBackendUnavailableError, PolicyDeniedError
from mcpinkscape.native_bridge import NativeBridgeClient
from mcpinkscape.storage import DocumentRoot, ensure_private_directory


DESKTOP_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR")


class InkscapeApplication:
    def __init__(self, access, bridge, root: DocumentRoot):
        self.access, self.bridge, self.root = access, bridge, root
        endpoint = Path(bridge.uds).expanduser().absolute()
        digest = hashlib.sha256(str(endpoint).encode()).hexdigest()[:16]
        self.directory = endpoint.parent / "managed-applications" / digest
        self.record_path = self.directory / "session.json"
        self._process = None

    def _client(self):
        return NativeBridgeClient(self.bridge.uds, min(self.bridge.timeout_seconds, 5),
                                  self.bridge.tcp_host, self.bridge.tcp_port)

    def _hello(self):
        try:
            return self._client().status()
        except LiveBackendUnavailableError:
            return None

    def _record(self):
        try:
            info = self.record_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PolicyDeniedError("managed application record is not a private owned file")
            if info.st_size > 16384:
                raise PolicyDeniedError("managed application record is too large")
            record = json.loads(self.record_path.read_text())
            if not re.fullmatch(r"[a-f0-9]{32}", record["session_id"]):
                raise ValueError("invalid session identifier")
            if record["application_id"] != "org.inkscape.Inkscape.mcp" + record["session_id"]:
                raise ValueError("invalid application identifier")
            return record
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyDeniedError("invalid managed application record") from exc

    @contextmanager
    def _locked(self):
        if os.name == "nt":
            raise LiveBackendUnavailableError("application lifecycle currently requires Unix and the session D-Bus")
        import fcntl
        ensure_private_directory(self.directory)
        descriptor = os.open(self.directory / "session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _write_record(self, record):
        temporary = self.directory / (uuid.uuid4().hex + ".json")
        with temporary.open("x") as handle:
            os.chmod(temporary, 0o600)
            json.dump(record, handle)
        temporary.replace(self.record_path)

    def _environment(self, application_id = None, for_launch = False):
        environment = os.environ.copy()
        for key, value in self.bridge.desktop_environment.items():
            environment.setdefault(key, value)
        record = self._record()
        if record and (application_id == record["application_id"] or for_launch):
            for key, value in record.get("desktop_environment", {}).items():
                if key not in DESKTOP_VARIABLES or not isinstance(value, str):
                    continue
                if for_launch:
                    environment.setdefault(key, value)
                else:
                    # A restarted MCP may inherit a different desktop or no
                    # graphical environment at all. Target the recorded session.
                    environment[key] = value
        return environment

    def _bus(self, application_id, method, *arguments):
        try:
            return subprocess.run([
                "gdbus", "call", "--session", "--dest", application_id,
                "--object-path", "/" + application_id.replace(".", "/"), "--method", method, *arguments,
            ], env = self._environment(application_id), capture_output = True, text = True, timeout = 3, check = False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _bus_pid(self, application_id):
        try:
            result = subprocess.run([
                "gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus", "--method",
                "org.freedesktop.DBus.GetConnectionUnixProcessID", application_id,
            ], env = self._environment(application_id), capture_output = True, text = True, timeout = 3, check = False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LiveBackendUnavailableError("cannot query the application session bus") from exc
        if result.returncode != 0 and ("NameHasNoOwner" in result.stderr or "ServiceUnknown" in result.stderr):
            return None
        match = re.fullmatch(r"\(uint32 (\d+),\)\s*", result.stdout) if result.returncode == 0 else None
        if not match:
            raise LiveBackendUnavailableError("cannot confirm application identity through the session bus: " +
                                              (result.stderr.strip() or repr(result.stdout)))
        return int(match[1])

    def _process_alive(self, record):
        try:
            if self._bus_pid(record["application_id"]) == record.get("pid"):
                return True
        except LiveBackendUnavailableError:
            pass
        # The process can exist before its GApplication bus name is registered.
        # Match the unique launch tag as well as the PID, avoiding PID reuse.
        try:
            result = subprocess.run(["ps", "-p", str(record["pid"]), "-o", "args="],
                                    capture_output = True, text = True, timeout = 3, check = False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and ("--app-id-tag=mcp" + record["session_id"]) in result.stdout.split()

    def managed_process_present(self):
        record = self._record()
        return bool(record and self._process_alive(record))

    def enable_bridge(self, force = False):
        """Activate only the named native Start action; never launch an application."""
        if os.name == "nt" or not self.access.allow_live_control or (not force and not self.bridge.auto_enable):
            return False
        record = self._record()
        targets = [record["application_id"]] if record else []
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        default_endpoint = state_home / "mcpinkscape/bridge.sock"
        if Path(self.bridge.uds).expanduser().absolute() == default_endpoint.absolute():
            targets.append("org.inkscape.Inkscape")
        for target in targets:
            described = self._bus(target, "org.gtk.Actions.Describe", "org.mcpinkscape.bridge.start")
            if described is None or described.returncode:
                continue
            activated = self._bus(target, "org.gtk.Actions.Activate", "org.mcpinkscape.bridge.start", "[]", "{}")
            if activated is not None and activated.returncode == 0:
                return True
        return False

    def _require_control(self):
        if not self.access.allow_live_control or not self.access.allow_application_control:
            raise PolicyDeniedError("configured policy forbids application control")
        if self.bridge.mode not in ("auto", "native_uds"):
            raise LiveBackendUnavailableError("application lifecycle requires the native bridge")
        if os.name == "nt":
            raise LiveBackendUnavailableError("application lifecycle currently requires Unix and the session D-Bus")

    def _owned(self, record, hello):
        owner = "api_key:" + self.access.id if hasattr(self.access, "id") else "stdio"
        return bool(record and hello and record.get("owner") == owner and
                    record.get("document_root") == str(self.root.root) and
                    record["session_id"] == hello.get("managed_session_id") and
                    record.get("pid") == hello.get("pid"))

    def status(self):
        self._require_control()
        hello = self._hello()
        if hello is None and self.enable_bridge():
            hello = self._hello()
        record = self._record()
        process_alive = bool(record and hello is None and self._process_alive(record))
        result = {"running": hello is not None or process_alive,
                  "bridge_available": hello is not None, "owned": self._owned(record, hello),
                  "pid": hello.get("pid") if hello else record.get("pid") if process_alive else None}
        if hello and "application.status" in hello.get("methods", []):
            result.update(self._client().request("application.status", {}))
        return result

    def start(self, path: str | None = None):
        self._require_control()
        source = self.root.resolve(path) if path is not None else None
        if source is not None and not source.is_file():
            raise InvalidDrawingValueError("startup document does not exist")
        with self._locked():
            self.enable_bridge(force = True)
            status = self.status()
            if status["bridge_available"]:
                if source is not None:
                    raise InvalidDrawingValueError("Inkscape is already running; start does not open an additional document")
                return {**status, "started": False}
            record = self._record()
            # A previous MCP may have exited while its GUI was still starting.
            # Resume its readiness wait rather than launching a second GUI.
            if record and self._process_alive(record):
                return self._wait_ready(record, started = False)
            session_id = uuid.uuid4().hex
            session_directory = ensure_private_directory(self.directory / session_id)
            profile = ensure_private_directory(session_directory / "profile")
            (profile / "preferences.xml").write_text(
                '<inkscape><group id="options"><group id="boot" showsplash="0"/></group></inkscape>')
            if source is None:
                source = session_directory / "empty.svg"
                source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"/>')
            env = self._environment(for_launch = True)
            for name in ("INKSCAPE_DATADIR", "LD_LIBRARY_PATH", "LD_PRELOAD"):
                env.pop(name, None)
            env.update({"INKSCAPE_PROFILE_DIR": str(profile), "MCPINKSCAPE_SESSION_ID": session_id,
                        "MCPINKSCAPE_DOCUMENT_ROOT": str(self.root.root),
                        "MCPINKSCAPE_BRIDGE_SOCKET": str(Path(self.bridge.uds).expanduser().absolute())})
            if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
                raise LiveBackendUnavailableError("launching Inkscape requires a graphical display")
            log_path = session_directory / "inkscape.log"
            try:
                with log_path.open("ab") as log:
                    self._process = subprocess.Popen([
                        self.bridge.executable, "--with-gui", "--app-id-tag=mcp" + session_id, str(source),
                    ], env = env, stdin = subprocess.DEVNULL, stdout = log, stderr = log, start_new_session = True)
            except OSError as exc:
                raise LiveBackendUnavailableError(f"cannot launch configured Inkscape: {exc}") from exc
            record = {"session_id": session_id, "pid": self._process.pid,
                      "application_id": "org.inkscape.Inkscape.mcp" + session_id,
                      "owner": "api_key:" + self.access.id if hasattr(self.access, "id") else "stdio",
                      "document_root": str(self.root.root),
                      "desktop_environment": {key: env[key] for key in DESKTOP_VARIABLES if key in env},
                      "log_path": str(log_path)}
            self._write_record(record)
            return self._wait_ready(record, started = True)

    def _wait_ready(self, record, started):
        deadline = time.monotonic() + self.bridge.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process is not None and self._process.pid == record["pid"] and self._process.poll() is not None:
                raise LiveBackendUnavailableError(f"Inkscape exited during startup; log: {record['log_path']}")
            self.enable_bridge(force = True)
            status = self.status()
            if status["bridge_available"]:
                if not status["owned"]:
                    raise LiveBackendUnavailableError("a different Inkscape instance owns the configured bridge")
                return {**status, "started": started}
            time.sleep(0.1)
        raise LiveBackendUnavailableError(f"Inkscape readiness timed out; session retained for recovery; log: {record['log_path']}")

    def shutdown(self, save_mode: str = "save"):
        self._require_control()
        if save_mode not in ("save", "refuse", "discard"):
            raise InvalidDrawingValueError("save_mode must be save, refuse, or discard")
        if save_mode == "save" and not self.access.allow_document_write:
            raise PolicyDeniedError("saving on shutdown requires document-write permission")
        with self._locked():
            self.enable_bridge(force = True)
            status = self.status()
            if not status["bridge_available"]:
                record = self._record()
                if record and self._process_alive(record):
                    raise LiveBackendUnavailableError("managed GUI is running but its bridge is unavailable")
                return {"stopped": True, "running": False, "saved_paths": []}
            record = self._record()
            hello = self._hello()
            if not self._owned(record, hello):
                raise PolicyDeniedError("shutdown is restricted to this access policy's managed application")
            result = self._client().request("application.shutdown", {
                "session_id": record["session_id"], "save_mode": save_mode,
            })
            if not result.get("accepted"):
                raise InvalidDrawingValueError(result.get("state", "application shutdown refused"))
            saved = []
            for path in result.get("saved_paths", []):
                try:
                    saved.append(str(Path(path).resolve().relative_to(self.root.root)))
                except ValueError as exc:
                    raise LiveBackendUnavailableError("invalid saved document receipt") from exc
            deadline = time.monotonic() + self.bridge.shutdown_timeout_seconds
            while time.monotonic() < deadline:
                if self._bus_pid(record["application_id"]) != record["pid"]:
                    if self._process is not None and self._process.pid == record["pid"]:
                        self._process.wait(timeout = max(0.1, deadline - time.monotonic()))
                    self.record_path.unlink(missing_ok = True)
                    return {"stopped": True, "running": False, "saved_paths": saved}
                time.sleep(0.1)
            raise LiveBackendUnavailableError("shutdown was accepted but application exit is not yet confirmed")

    def release(self):
        self._require_control()
        with self._locked():
            if not self.status()["owned"]:
                raise PolicyDeniedError("only an owned application can be handed to the user")
            self._write_record({**self._record(), "owner": None})
            return {"released": True, "running": True, "owned": False}
