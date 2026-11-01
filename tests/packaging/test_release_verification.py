from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packaging'))
spec = importlib.util.spec_from_file_location('verify_installation', ROOT / 'packaging/verify_installation.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
sys.path.pop(0)


def artifact(path, module):
    path.mkdir()
    (path / verifier.driver.MARKER).write_text(json.dumps({'application': verifier.driver.APPLICATION}))
    extension = path / 'share/inkscape/extensions'
    extension.mkdir(parents=True)
    (extension / 'libmcpinkscape_bridge.so').write_bytes(module)
    return path


def test_existing_work_and_input_report_are_refused_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier.sys, 'platform', 'linux')
    previous = artifact(tmp_path / 'previous', b'old')
    current = artifact(tmp_path / 'current', b'new')
    work = tmp_path / 'work'
    work.mkdir()
    sentinel = work / 'user-file'
    sentinel.write_text('keep')
    with pytest.raises(FileExistsError):
        verifier.qualify(previous, current, work, tmp_path / 'report.json')
    with pytest.raises(RuntimeError, match='outside the input'):
        verifier.qualify(previous, current, tmp_path / 'new-work', current / 'report.json')
    assert sentinel.read_text() == 'keep'
    assert not (tmp_path / 'new-work').exists()
    assert not (current / 'report.json').exists()


def test_real_install_failure_is_journaled_and_preserves_input_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier.sys, 'platform', 'linux')
    previous = artifact(tmp_path / 'previous', b'old')
    current = artifact(tmp_path / 'current', b'new')
    before = verifier.manifest(previous), verifier.manifest(current)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'user-desktop'))
    work, report = tmp_path / 'work', tmp_path / 'report.json'
    with pytest.raises(RuntimeError, match='Installed executable missing'):
        verifier.qualify(previous, current, work, report)
    evidence = json.loads(report.read_text())
    assert evidence['status'] == 'failed'
    assert evidence['checks'] == {'distinct_artifacts': True}
    assert not (work / 'application').exists()
    assert (verifier.manifest(previous), verifier.manifest(current)) == before
    assert not (tmp_path / 'user-desktop').exists()


def test_hidden_artifacts_restore_after_verification_error(tmp_path):
    source = tmp_path / 'artifact'
    source.mkdir()
    (source / 'file').write_text('keep')
    state = {}
    with pytest.raises(ValueError, match='GUI failed'):
        with verifier.hidden_paths((source,), state, lambda: None):
            assert not source.exists()
            raise ValueError('GUI failed')
    assert (source / 'file').read_text() == 'keep'
    assert state['hidden_paths_restored'] is True
    assert not (tmp_path / 'artifact.qualification-hidden').exists()


def test_hidden_artifact_refuses_an_existing_dangling_symlink(tmp_path):
    source = tmp_path / 'artifact'
    source.mkdir()
    hidden = tmp_path / 'artifact.qualification-hidden'
    hidden.symlink_to(tmp_path / 'absent')
    with pytest.raises(RuntimeError, match='already exists'):
        with verifier.hidden_paths((source,), {}, lambda: None):
            pytest.fail('Existing neighbor was overwritten')
    assert source.is_dir()
    assert hidden.is_symlink()
