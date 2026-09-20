"""Install Sweetmeter for the current user, preserving only their own state.

Without --app, install this source checkout and its dependencies. --app accepts
an extracted native Sweetmeter.app (macOS) or Sweetmeter folder (Windows/Linux).
Never copies state, account files, development caches or private signing keys.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from meter.paths import app_command, data_dir, default_state_dir, install_root

from meter.installation import startup, install_native, retire_legacy_startup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--no-startup', action='store_true')
    parser.add_argument('--remove-startup', action='store_true')
    args = parser.parse_args()
    if args.remove_startup:
        startup([], enable=False)
        return
    os.umask(0o077)
    root = data_dir()
    state = default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    if args.app:
        destination = install_native(args.app, start_at_login=not args.no_startup)
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
        python = runtime / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        if not python.is_file():
            subprocess.run([sys.executable, '-m', 'venv', str(runtime / '.venv')], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(runtime / 'requirements.txt')], check=True)
        command = [str(python), '-m', 'meter']
        # `-m meter` needs an explicit portable source launcher outside the checkout.
        launcher = runtime / 'run.py'
        launcher.write_text('from meter.__main__ import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
        command = [str(python), str(launcher)]
        info = {'kind': 'source', 'root': str(runtime)}
    (root / 'install.json').write_text(json.dumps(info) + '\n', encoding='utf-8')
    if not args.no_startup:
        retire_legacy_startup()
        startup(command + ['--background', '--state-dir', str(state)])
    print('Installed:', info['root'])
    print('State preserved:', state)
    print('Start:', subprocess.list2cmdline(command))


if __name__ == '__main__':
    main()
