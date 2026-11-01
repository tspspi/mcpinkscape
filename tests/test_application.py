from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcpinkscape.application import InkscapeApplication
from mcpinkscape.config.schema import AccessConfig, APIKeyConfig, BridgeConfig
from mcpinkscape.errors import InvalidDrawingValueError, LiveBackendUnavailableError, PolicyDeniedError
from mcpinkscape.storage import DocumentRoot


def controller(tmp_path, access=None):
    return InkscapeApplication(access or AccessConfig(), BridgeConfig(uds=str(tmp_path / "bridge.sock")),
                               DocumentRoot(tmp_path / "library"))


def record(application):
    return {"session_id": "a" * 32, "pid": 1234,
            "application_id": "org.inkscape.Inkscape.mcp" + "a" * 32,
            "owner": "stdio", "document_root": str(application.root.root), "log_path": "fixture.log"}


def test_application_control_requires_explicit_remote_permission(tmp_path):
    access = APIKeyConfig(id="remote")
    application = controller(tmp_path, access)
    assert not access.allow_application_control
    for operation in (application.start, application.shutdown, application.release, application.status):
        with pytest.raises(PolicyDeniedError):
            operation()
    assert not application.directory.exists()


def test_ownership_requires_session_pid_root_and_access_identity(tmp_path):
    application = controller(tmp_path)
    saved = record(application)
    hello = {"pid": 1234, "managed_session_id": "a" * 32}
    assert application._owned(saved, hello)
    for key, value in (("pid", 1235), ("session_id", "b" * 32),
                       ("document_root", str(tmp_path)), ("owner", "another-key")):
        assert not application._owned({**saved, key: value}, hello)


def test_handoff_persists_bridge_identity_without_shutdown_ownership(tmp_path, monkeypatch):
    application = controller(tmp_path)
    with application._locked():
        application._write_record(record(application))
    monkeypatch.setattr(application, "status", lambda: {"owned": True})
    assert application.release()["released"]
    saved = application._record()
    assert saved["owner"] is None
    assert saved["application_id"].endswith("a" * 32)
    assert not application._owned(saved, {"pid": 1234, "managed_session_id": "a" * 32})


def test_start_reuses_launch_that_has_not_registered_on_dbus_yet(tmp_path, monkeypatch):
    application = controller(tmp_path)
    saved = record(application)
    with application._locked():
        application._write_record(saved)
    monkeypatch.setattr(application, "status", lambda: {"running": True, "bridge_available": False})
    monkeypatch.setattr(application, "_process_alive", lambda entry: entry == saved)
    monkeypatch.setattr(application, "_wait_ready", lambda entry, started: {"started": started, "pid": entry["pid"]})
    assert application.start() == {"started": False, "pid": 1234}


def test_record_rejects_symlink_and_unsafe_permissions(tmp_path):
    application = controller(tmp_path)
    with application._locked():
        target = application.directory / "foreign.json"
        target.write_text(json.dumps(record(application)))
        target.chmod(0o600)
        application.record_path.symlink_to(target)
        with pytest.raises(PolicyDeniedError):
            application._record()
        application.record_path.unlink()
        target.replace(application.record_path)
        application.record_path.chmod(0o644)
        with pytest.raises(PolicyDeniedError):
            application._record()


def test_start_path_and_shutdown_mode_validate_before_launch(tmp_path):
    application = controller(tmp_path)
    with pytest.raises(PolicyDeniedError):
        application.start("../outside.svg")
    with pytest.raises(InvalidDrawingValueError):
        application.shutdown("unknown")
    assert not application.directory.exists()


def test_custom_socket_never_activates_unrelated_desktop(tmp_path, monkeypatch):
    application = controller(tmp_path)
    calls = []
    monkeypatch.setattr(application, "_bus", lambda *args: calls.append(args))
    assert not application.enable_bridge()
    assert not calls


def test_unreachable_bus_is_not_proof_of_application_exit(tmp_path, monkeypatch):
    application = controller(tmp_path)
    monkeypatch.setattr("mcpinkscape.application.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to session bus"))
    with pytest.raises(LiveBackendUnavailableError, match="cannot confirm"):
        application._bus_pid("org.inkscape.Inkscape")


def test_shutdown_keeps_record_when_exit_cannot_be_confirmed(tmp_path, monkeypatch):
    application = controller(tmp_path)
    with application._locked():
        application._write_record(record(application))
    monkeypatch.setattr(application, "enable_bridge", lambda **kwargs: True)
    monkeypatch.setattr(application, "status", lambda: {"running": True, "bridge_available": True, "owned": True})
    monkeypatch.setattr(application, "_hello", lambda: {"pid": 1234, "managed_session_id": "a" * 32})
    monkeypatch.setattr(application, "_client", lambda: SimpleNamespace(request=lambda *args:
                        {"accepted": True, "saved_paths": []}))
    def unavailable(*args):
        raise LiveBackendUnavailableError("session bus unavailable")
    monkeypatch.setattr(application, "_bus_pid", unavailable)
    with pytest.raises(LiveBackendUnavailableError):
        application.shutdown()
    assert application.record_path.exists()


def test_restart_recovers_recorded_desktop_environment(tmp_path, monkeypatch):
    application = controller(tmp_path)
    saved = {**record(application), "desktop_environment": {
        "DISPLAY": ":fixture", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/fixture-bus",
        "LD_PRELOAD": "must-not-be-used",
    }}
    with application._locked():
        application._write_record(saved)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    environment = application._environment(saved["application_id"])
    assert environment["DISPLAY"] == ":fixture"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private/fixture-bus"
    assert "LD_PRELOAD" not in environment
