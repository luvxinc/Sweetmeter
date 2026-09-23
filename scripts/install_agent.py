"""Install Sweetmeter for the current user, preserving only their own state.

Without --app, install this source checkout into a private runtime with its
own virtual environment; dependencies are installed there exactly once and
again only when requirements.txt or the interpreter changes. --app accepts an
extracted native Sweetmeter.app (macOS) or Sweetmeter folder (Windows/Linux).
--uninstall removes the installation (add --remove-data to delete state too).
Never copies state, account files, development caches or private signing keys.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from meter.paths import data_dir, default_state_dir

from meter.installation import InstallError, startup, install_native, retire_legacy_startup, uninstall


def interpreter_identity(python):
    """Exact interpreter a venv runs (path, version, ABI); None if it cannot start."""
    try:
        result = subprocess.run([str(python), '-c', 'import sys; print(sys.executable); print(sys.version); '
                                 'print(sys.implementation.cache_tag)'], capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode:
        return None
    return str(python) + '\n' + result.stdout.strip()


def install_requirements(python, requirements, marker, identity=None):
    """Run pip only when the pinned requirements or the interpreter changed.

    The marker covers this installer's Python, the venv interpreter's path and
    version, and requirements.txt, so an upgraded interpreter reinstalls.
    """
    identity = identity if identity is not None else interpreter_identity(python)
    digest = hashlib.sha256(b'\0'.join([requirements.read_bytes(), str(identity).encode('utf-8'),
                                        sys.version.encode('utf-8')])).hexdigest()
    try:
        if marker.read_text(encoding='ascii').strip() == digest:
            return False
    except OSError:
        pass
    marker.unlink(missing_ok=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(requirements)], check=True)
    marker.write_text(digest + '\n', encoding='ascii')
    return True


def ensure_venv(folder):
    """Create the runtime venv, recreating it if its interpreter vanished or changed."""
    python = folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    identity = interpreter_identity(python) if python.is_file() else None
    if identity is None or (sys.version.split()[0] not in identity):
        subprocess.run([sys.executable, '-m', 'venv', '--clear', str(folder)], check=True)
        identity = interpreter_identity(python)
        if identity is None:
            raise SystemExit('Could not create a working virtual environment for Sweetmeter.')
    return python, identity


def _record_startup_choice(enabled):
    """Remember an explicit choice so a later repair does not undo it."""
    path = data_dir() / 'install.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if isinstance(record, dict) and record.get('startup') != enabled:
        temporary = path.with_name('install.json.tmp')
        temporary.write_text(json.dumps(dict(record, startup=enabled)) + '\n', encoding='utf-8')
        os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--no-startup', action='store_true')
    parser.add_argument('--remove-startup', action='store_true')
    parser.add_argument('--uninstall', action='store_true', help='Remove this installation and its startup entry')
    parser.add_argument('--remove-data', action='store_true', help='With --uninstall: also delete state')
    args = parser.parse_args()
    os.umask(0o077)  # Install record, runtime and state are private to this user.
    if args.remove_startup:
        startup([], enable=False)
        _record_startup_choice(False)
        return
    if args.uninstall:
        try:
            for path in uninstall(args.remove_data):
                print('Removed:', path)
        except InstallError as error:
            raise SystemExit(str(error))
        return
    root = data_dir()
    state = default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    if args.app:
        try:
            destination = install_native(args.app, start_at_login=not args.no_startup)
        except (InstallError, ValueError, OSError) as error:
            raise SystemExit('Sweetmeter setup did not finish: ' + str(error))
        print('Installed:', destination)
        return
    else:
        if sys.version_info < (3, 11):
            raise SystemExit('Python 3.11+ with Tk is required for source installation.')
        import tkinter  # Fail early with the real missing dependency.
        runtime = root / 'runtime'
        runtime.mkdir(exist_ok=True)
        shutil.copytree(SOURCE / 'meter', runtime / 'meter', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__'))
        for filename in ('VERSION', 'requirements.txt', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
            shutil.copy2(SOURCE / filename, runtime / filename)
        python, identity = ensure_venv(runtime / '.venv')
        install_requirements(python, runtime / 'requirements.txt', runtime / '.venv' / 'sweetmeter-requirements.sha256',
                             identity)
        # Fail here, with the real error, rather than silently at login.
        subprocess.run([str(python), '-m', 'meter', '--self-test'], cwd=str(runtime), check=True)
        # `-m meter` needs an explicit portable source launcher outside the checkout.
        launcher = runtime / 'run.py'
        launcher.write_text('from meter.__main__ import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
        command = [str(python), str(launcher)]
        info = {'kind': 'source', 'root': str(runtime), 'startup': not args.no_startup, 'command': command}
    temporary = root / 'install.json.tmp'
    temporary.write_text(json.dumps(info) + '\n', encoding='utf-8')
    os.replace(temporary, root / 'install.json')
    if not args.no_startup:
        retire_legacy_startup()
        startup(command + ['--background', '--state-dir', str(state)])
    else:
        startup([], enable=False)  # --no-startup also removes an entry an earlier install created.
    print('Installed:', info['root'])
    print('State preserved:', state)
    print('Start:', subprocess.list2cmdline(command))


if __name__ == '__main__':
    main()
