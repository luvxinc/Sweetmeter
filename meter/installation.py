"""Per-user native installation and startup registration (no administrator access)."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from .paths import app_command, data_dir, default_state_dir, install_root, resource_root

LABEL = 'com.sweetmeter.companion'
PROFILE_ENV = ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'SWEETMETER_CODEX_PATH',
               'CLAUDE_SECURESTORAGE_CONFIG_DIR')


def retire_legacy_startup():
    """Only retire launch agents proven to execute this adopted prototype path."""
    if sys.platform != 'darwin' or data_dir().name != 'QuotaMeter':
        return
    adopted = data_dir().absolute()
    for path in (Path.home() / 'Library/LaunchAgents').glob('*.plist'):
        try:
            info = plistlib.loads(path.read_bytes())
            arguments = info.get('ProgramArguments', [])
            executable = Path(arguments[0]).absolute() if arguments else None
            label = info.get('Label', '')
            if (info.get('WorkingDirectory') != str(adopted) or executable is None
                    or not executable.is_relative_to(adopted) or '-m' not in arguments
                    or 'meter' not in arguments or not isinstance(label, str) or not label):
                continue
        except (OSError, ValueError, TypeError):
            continue
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/' + label], capture_output=True)
        backup = adopted / 'retired-startup'
        backup.mkdir(parents=True, exist_ok=True)
        path.replace(backup / path.name)


def native_startup_command():
    """Launcher lives outside the replaceable app and repairs interrupted swaps."""
    suffix = '.exe' if sys.platform == 'win32' else ''
    source = resource_root() / ('sweetmeter-update-helper' + suffix)
    folder = data_dir() / 'launcher'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ('sweetmeter-launcher' + suffix)
    temporary = target.with_suffix(target.suffix + '.new')
    shutil.copy2(source, temporary)
    temporary.chmod(0o700)
    temporary.replace(target)
    return [str(target), '--launch']


def startup(command, *, enable=True):
    if sys.platform == 'darwin':
        destination = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
        if not enable:
            destination.unlink(missing_ok=True)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        for key in PROFILE_ENV:
            if key in os.environ:
                environment[key] = os.environ[key]
        config = dict(Label=LABEL, ProgramArguments=command, RunAtLoad=True,
                      WorkingDirectory=str(data_dir()), ThrottleInterval=30,
                      EnvironmentVariables=environment,
                      StandardOutPath=str(default_state_dir() / 'agent.log'),
                      StandardErrorPath=str(default_state_dir() / 'agent.log'))
        destination.write_bytes(plistlib.dumps(config))
        subprocess.run(['launchctl', 'bootstrap', domain, str(destination)], check=True)
    elif sys.platform == 'win32':
        folder = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup'
        destination = folder / 'Sweetmeter.vbs'
        if not enable:
            destination.unlink(missing_ok=True)
            return
        folder.mkdir(parents=True, exist_ok=True)
        quoted = subprocess.list2cmdline(command).replace('"', '""')
        lines = ['Set shell = CreateObject("WScript.Shell")']
        for key in PROFILE_ENV:
            if key in os.environ:
                value = os.environ[key].replace('"', '""')
                if '\n' in value or '\r' in value:
                    raise ValueError('Profile paths cannot contain newlines')
                lines.append(f'shell.Environment("PROCESS")("{key}") = "{value}"')
        lines.append(f'shell.Run "{quoted}", 0, False')
        destination.write_text('\n'.join(lines) + '\n', encoding='utf-16')
        subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        destination = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart/sweetmeter.desktop'
        if not enable:
            destination.unlink(missing_ok=True)
            return
        # Desktop-entry quoting differs from shell quoting; escape reserved syntax.
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
        destination.parent.mkdir(parents=True, exist_ok=True)
        assignments = [f'{key}={os.environ[key]}' for key in PROFILE_ENV if key in os.environ]
        if any('\n' in value or '\r' in value for value in assignments + command):
            raise ValueError('Startup paths cannot contain newlines')
        desktop_command = ['env', *assignments, *command] if assignments else command
        destination.write_text('[Desktop Entry]\nType=Application\nName=Sweetmeter\n'
                               'Exec=' + ' '.join(map(quote, desktop_command)) + '\nTerminal=false\n', encoding='utf-8')
        subprocess.Popen(command, start_new_session=True)


def install_native(application, *, start_at_login=True):
    source = Path(application).absolute()
    destination = install_root().absolute()
    expected = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
    if source.name != expected or not Path(app_command(source)[0]).is_file():
        raise ValueError('Choose the extracted native Sweetmeter application folder.')
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('Refusing symlinked installation path.')
    if destination.exists():
        raise ValueError('Installation already exists; use the app updater or move the old copy aside after quitting.')
    from .self_update import validate_tree
    validate_tree(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    root = data_dir()
    default_state_dir().mkdir(parents=True, exist_ok=True)
    (root / 'install.json').write_text(json.dumps({'kind': 'native', 'root': str(destination)}) + '\n')
    command = native_startup_command()
    if start_at_login:
        retire_legacy_startup()
        startup(command + ['--background', '--state-dir', str(default_state_dir())])
    return destination


def install_current(*, start_at_login=True):
    if not getattr(sys, 'frozen', False):
        raise ValueError('Source installations use scripts/install_agent.py.')
    executable = Path(sys.executable).absolute()
    application = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    return install_native(application, start_at_login=start_at_login)
