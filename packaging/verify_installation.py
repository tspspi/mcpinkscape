#!/usr/bin/env python3
"""Exercise two real Linux application artifacts in a new disposable directory.

Requires the live-verification prerequisites and Python development dependencies.
Input contents are read-only. Only installations created inside --work-dir are replaced
or removed; existing work directories are refused. This does not qualify pkg
transactions on FreeBSD or Windows installation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import build_native as driver

ROOT = Path(__file__).resolve().parents[1]


def manifest(directory):
    """Record file contents, symlink targets and executable modes separately."""
    result = {}
    for path in sorted(directory.rglob('*')):
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            result[relative] = {'symlink': os.readlink(path)}
        elif path.is_file():
            result[relative] = {'sha256': driver.digest(path), 'mode': path.stat().st_mode & 0o777}
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def hidden_paths(paths, state, save):
    """Explicitly hide known build/artifact paths and restore them on failure."""
    moved = []
    state['hidden_paths_restored'] = False
    save()
    try:
        for path in paths:
            hidden = path.with_name(path.name + '.qualification-hidden')
            require(not os.path.lexists(hidden), f'Hidden-path destination already exists: {hidden}')
            state.setdefault('temporarily_hidden', []).append({'original': str(path), 'hidden': str(hidden)})
            save()
            path.rename(hidden)
            moved.append((path, hidden))
        yield
    finally:
        for original, hidden in reversed(moved):
            require(not os.path.lexists(original), f'Original path unexpectedly recreated; restore manually: {hidden}')
            hidden.rename(original)
        state['hidden_paths_restored'] = len(moved) == len(paths)
        save()


def qualify(previous, current, work, report, hide=()):
    require(sys.platform.startswith('linux'), 'This verifier qualifies Linux managed prefixes only')
    require(previous != current, 'Provide separate previous and current artifacts')
    for source in (previous, current):
        require(driver.owned(source), f'Not a managed application artifact: {source}')
        require(work != source and work not in source.parents and source not in work.parents,
                'Work directory and input artifacts must be separate')
    require(not os.path.lexists(report), 'Refusing to overwrite an existing report')
    for source in (previous, current):
        require(report != source and report not in source.parents and source not in report.parents,
                'Report must be outside the input artifacts')
    require(report != work and report not in work.parents,
            'Report must not be an ancestor of the work directory')
    for path in hide:
        require(path.exists(), f'Path to hide does not exist: {path}')
        require(path != ROOT and path not in ROOT.parents and ROOT not in path.parents,
                'Cannot hide the running verifier source tree')
        require(path != work and path not in work.parents and work not in path.parents,
                'Cannot hide qualification work')
        require(path != report and path not in report.parents,
                'Cannot hide the report destination')
        require(path in (previous, current) or (
            (path / 'native-inputs.json').is_file() and (path / 'cmake/CMakeCache.txt').is_file()),
            f'Only explicit input artifacts or a native build directory may be hidden: {path}')
    require(len(set(hide)) == len(hide) and not any(
        a in b.parents for a in hide for b in hide if a != b), 'Hidden paths must be separate')
    work.mkdir(parents=True, exist_ok=False)
    state = {'status': 'running', 'started': time.time(), 'platform': sys.platform,
             'previous_artifact': str(previous), 'current_artifact': str(current), 'checks': {}}

    def save():
        report.parent.mkdir(parents=True, exist_ok=True)
        temporary = report.with_suffix(report.suffix + '.tmp')
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.replace(report)

    def passed(name):
        state['checks'][name] = True
        save()

    save()
    original_data_home = os.environ.get('XDG_DATA_HOME')
    os.environ['XDG_DATA_HOME'] = str(work / 'desktop')
    try:
        old_files, new_files = manifest(previous), manifest(current)
        module = 'share/inkscape/extensions/libmcpinkscape_bridge.so'
        require(module in old_files and module in new_files, 'Both artifacts must contain the native bridge')
        require(old_files[module] != new_files[module], 'Upgrade requires distinct native bridge artifacts')
        state['previous_files'] = old_files
        state['current_files'] = new_files
        passed('distinct_artifacts')
        neighbor = work / 'unrelated.txt'
        neighbor.write_text('unrelated application: preserve this file\n')
        prefix = work / 'application'
        backup = work / 'application.previous'
        desktop = work / 'desktop/applications/mcpinkscape.desktop'

        def matching(directory, expected):
            require(manifest(directory) == expected, f'Installed files differ: {directory}')

        def gui(directory, name, *, full):
            output = work / (name + '.json')
            verifier = 'verify_mcp_lifecycle.py' if full else 'verify_live.py'
            env = os.environ.copy()
            env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
            # An older artifact must not accidentally load resources from the
            # still-present original current installation's configured prefix.
            with hidden_paths(hide, state, save):
                driver.verify(directory)
                subprocess.run([sys.executable, str(ROOT / 'packaging' / verifier),
                                str(directory / 'bin/inkscape'), '--output', str(output)],
                               cwd=ROOT, env=env, check=True, timeout=360)
                state.setdefault('gui_reports', {})[name] = json.loads(output.read_text())
            passed(name)

        driver.install(previous, prefix)
        matching(prefix, old_files)
        require(not backup.exists(), 'Clean installation unexpectedly created a backup')
        passed('clean_install')
        driver.install(current, prefix)
        matching(prefix, new_files)
        matching(backup, old_files)
        passed('upgrade')
        gui(prefix, 'installed_lifecycle', full=True)

        # A real invalid application exercises install's verification failure,
        # without replacing or mocking its verification routine.
        invalid = work / 'invalid-artifact'
        invalid.mkdir()
        (invalid / driver.MARKER).write_text((current / driver.MARKER).read_text())
        desktop_before = desktop.read_bytes()
        try:
            driver.install(invalid, prefix)
        except RuntimeError as error:
            require('Installed executable missing:' in str(error), f'Unexpected installation failure: {error}')
        else:
            raise RuntimeError('Invalid application unexpectedly installed')
        matching(prefix, new_files)
        matching(backup, old_files)
        require(desktop.read_bytes() == desktop_before, 'Failed installation changed the desktop entry')
        passed('failed_upgrade_recovery')

        # install copies the backup into incoming before replacing either path.
        driver.install(backup, prefix)
        matching(prefix, old_files)
        matching(backup, new_files)
        gui(prefix, 'rollback_live', full=False)
        passed('rollback')
        driver.install(current, prefix)
        matching(prefix, new_files)
        matching(backup, old_files)
        passed('upgrade_after_rollback')

        relocated = work / 'relocated application'
        prefix.rename(relocated)
        require(not prefix.exists(), 'Original installation path still exists')
        matching(relocated, new_files)
        gui(relocated, 'relocated_lifecycle', full=True)
        passed('relocation')
        # Re-register through the normal installation path, then remove it.
        # The original backup is removed using its original managed prefix.
        driver.install(relocated, prefix)
        driver.uninstall(prefix)
        driver.uninstall(relocated)
        require(not prefix.exists() and not backup.exists() and not relocated.exists(),
                'Removal left a managed installation behind')
        require(not desktop.exists(), 'Removal left the matching desktop entry behind')
        require(neighbor.read_text() == 'unrelated application: preserve this file\n',
                'An unrelated neighboring file was changed')
        passed('removal_preserves_neighbors')
        require(manifest(previous) == old_files and manifest(current) == new_files,
                'Qualification changed an input artifact')
        passed('input_artifacts_unchanged')
        state['status'] = 'passed'
    except BaseException as error:
        state.update(status='failed', error=str(error))
        raise
    finally:
        state['finished'] = time.time()
        save()
        if original_data_home is None:
            os.environ.pop('XDG_DATA_HOME', None)
        else:
            os.environ['XDG_DATA_HOME'] = original_data_home
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-artifact', type=Path, required=True)
    parser.add_argument('--current-artifact', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--temporarily-hide', type=Path, action='append', default=[],
                        help='Explicit input artifact or build directory to hide during GUI checks, then restore')
    args = parser.parse_args()
    qualify(*(path.expanduser().resolve() for path in
              (args.previous_artifact, args.current_artifact, args.work_dir, args.output)),
            hide=tuple(path.expanduser().resolve() for path in args.temporarily_hide))


if __name__ == '__main__':
    main()
