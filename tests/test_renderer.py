from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcpinkscape.errors import LiveActionUnsupportedError
from mcpinkscape.renderer import InkscapeCLI


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[1] == "--version":
            return subprocess.CompletedProcess(arguments, 0, "Inkscape 1.4.3\n", "")
        if arguments[1] == "--action-list":
            return subprocess.CompletedProcess(arguments, 0, "select-by-id : select\nselect-clear : clear\nselect-list : list\nobject-set-attribute : style\ntransform-translate : move\nexport-type : type\nexport-area-page : area\nexport-filename : file\nexport-do : do\n", "")
        if "select-list" in arguments[-1]:
            return subprocess.CompletedProcess(arguments, 0, "rect-1 cloned: false ref: 1 href: 0 total href: 0\n", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")


def test_capability_probe_and_unsupported_action():
    runner = FakeRunner()
    cli = InkscapeCLI("inkscape", runner = runner)
    assert cli.capabilities.version == "Inkscape 1.4.3"
    with pytest.raises(LiveActionUnsupportedError):
        cli.active_actions(["file-save"])
    receipt = cli.active_set_style(["rect-1"], {"fill": "#f00"})
    assert "--active-window" in runner.calls[-1]
    assert receipt["selection_observed"] is True
    assert receipt["selected_ids"] == ["rect-1"]


def test_active_action_adapter_rejects_action_string_injection():
    cli = InkscapeCLI("inkscape", runner = FakeRunner())
    with pytest.raises(LiveActionUnsupportedError):
        cli.active_select(["rect-1;quit"])
    with pytest.raises(LiveActionUnsupportedError):
        cli.active_set_style(["rect-1"], {"fill": "#f00;quit"})
    with pytest.raises(LiveActionUnsupportedError):
        cli.active_transform(["rect-1"], "move", [float("nan"), 1])


def test_active_selection_inspection_uses_version_advertised_select_list():
    cli = InkscapeCLI("inkscape", runner = FakeRunner())
    assert cli.active_list_selection()["selected_ids"] == ["rect-1"]
