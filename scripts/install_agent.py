"""Install/update this meter's per-user macOS background process."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from build_bluetooth import build

SOURCE = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / 'Library/Application Support/QuotaMeter'
LABEL = 'com.sweetmeter.companion'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
DOMAIN = f'gui/{os.getuid()}'


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This legacy installer supports macOS only.')
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['launchctl', 'bootout', DOMAIN + '/' + LABEL], capture_output=True)
    build(SOURCE, RUNTIME)
    shutil.copytree(SOURCE / 'meter', RUNTIME / 'meter', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    (RUNTIME / 'meter/transport.py').unlink(missing_ok=True)  # Remove the retired USB transport.
    shutil.copy2(SOURCE / 'requirements.txt', RUNTIME / 'requirements.txt')
    python = RUNTIME / '.venv/bin/python'
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(RUNTIME / '.venv')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(RUNTIME / 'requirements.txt')], check=True)
    state = RUNTIME / 'state'
    state.mkdir(exist_ok=True)
    # Keep this installation's state on upgrades; never import checkout caches.
    environment = {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'PYTHONUNBUFFERED': '1'}
    for key in ['CLAUDE_CONFIG_DIR', 'CODEX_HOME']:
        if key in os.environ:
            environment[key] = os.environ[key]
    config = dict(Label=LABEL, ProgramArguments=[str(python), '-m', 'meter', '--state-dir', str(state)],
                  WorkingDirectory=str(RUNTIME), RunAtLoad=True, KeepAlive=True, ThrottleInterval=60,
                  EnvironmentVariables=environment,
                  StandardOutPath=str(state / 'agent.log'), StandardErrorPath=str(state / 'agent.log'))
    with PLIST.open('wb') as handle:
        plistlib.dump(config, handle)
    subprocess.run(['launchctl', 'bootstrap', DOMAIN, str(PLIST)], check=True)
    print('Installed:', PLIST)
    print('Runtime:', RUNTIME)


if __name__ == '__main__':
    main()
