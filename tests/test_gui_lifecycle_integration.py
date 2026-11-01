"""Opt-in, self-contained GUI verification; never attaches to a user's session."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    not os.environ.get("MCPINKSCAPE_GUI_VERIFY_EXECUTABLE"),
    reason="set MCPINKSCAPE_GUI_VERIFY_EXECUTABLE to an ABI-matched Inkscape to run private GUI tests",
)
def test_mcp_first_startup_bridge_restart_and_gui_relaunch(tmp_path):
    root = Path(__file__).resolve().parents[1]
    report = tmp_path / "lifecycle.json"
    result = subprocess.run([
        sys.executable, str(root / "packaging/verify_mcp_lifecycle.py"),
        os.environ["MCPINKSCAPE_GUI_VERIFY_EXECUTABLE"], "--output", str(report),
    ], cwd=root, text=True, capture_output=True, timeout=300)
    log = report.with_suffix(".log")
    assert result.returncode == 0, result.stdout + result.stderr + (log.read_text() if log.exists() else "")
    evidence = json.loads(report.read_text())
    assert evidence["mcp_before_gui"]
    assert evidence["late_bridge_connection"]
    assert evidence["bridge_restart"]
    assert evidence["gui_relaunch_same_mcp_session"]
    assert evidence["native_integration"]
    assert evidence["active_window_integration"]
    assert evidence["clean_gui_exit"]
    assert evidence["document_targeting"] and all(evidence["document_targeting"].values())
    for scenario in ("automatic_startup", "stdio_fresh_start", "startup_document",
                     "automatic_bridge_activation", "gui_before_mcp",
                     "mcp_process_restart", "gui_crash_recovery", "save_on_shutdown",
                     "refuse_unsaved_shutdown", "explicit_discard", "interactive_handoff",
                     "unmanaged_shutdown_refused"):
        assert evidence[scenario], scenario
