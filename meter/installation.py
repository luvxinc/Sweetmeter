"""Per-user native installation, startup registration, repair and uninstall.

No administrator access. Only locations listed by `paths.candidate_install_roots`
and files this module creates are ever adopted, rewritten or removed.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .paths import (BUNDLE_ID, app_command, candidate_install_roots, data_dir, default_install_root,
                    default_state_dir, install_root)

LABEL = 'com.sweetmeter.companion'
PROFILE_ENV = ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'SWEETMETER_CODEX_PATH',
               'CLAUDE_SECURESTORAGE_CONFIG_DIR')
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Sweetmeter'
RUN_VALUE = 'Sweetmeter'
# Files this module (or the updater) creates directly under data_dir().
MANAGED_FILES = ('install.json', 'startup-environment.json', 'companion-swap.json', 'helper.log',
                 'helper.log.1', 'install-error.txt')


class InstallError(RuntimeError):
    """A plain, user-facing installation problem (never shown as a traceback)."""


def read_install_record():
    path = data_dir() / 'install.json'
    try:
        if path.stat().st_size > 16384:
            return {}
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


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


LOCK_TIMEOUT = 60


@contextmanager
def install_lock(timeout=None):
    """Serialize installers, repairs and uninstallers for this user."""
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / 'install.lock', 'a+b')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise InstallError('Another Sweetmeter setup is still running. '
                                       'Wait for it to finish, then try again.') from None
                time.sleep(.25)
        try:
            yield
        finally:
            if sys.platform == 'win32':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _write_file(path, data):
    """Atomic replace; returns False when the file already had exactly this content."""
    path = Path(path)
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return True


def looks_like_sweetmeter(root):
    """A recognizable Sweetmeter application tree (not merely a folder with that name)."""
    root = Path(root)
    if root.is_symlink() or not Path(app_command(root)[0]).is_file():
        return False
    try:
        if sys.platform == 'darwin':
            info = plistlib.loads((root / 'Contents/Info.plist').read_bytes())
            return info.get('CFBundleIdentifier') == BUNDLE_ID
        metadata = root / '_internal/build-metadata.json'
        if metadata.is_file():
            return json.loads(metadata.read_text(encoding='utf-8')).get('kind') == 'sweetmeter-companion-build'
        return (root / '_internal/VERSION').is_file()
    except (OSError, ValueError, AttributeError):
        return False


def _is_managed(record, root):
    return record.get('kind') == 'native' and record.get('root') == str(root)


def native_startup_command(root=None):
    """Launcher lives outside the replaceable app and repairs interrupted swaps."""
    from .self_update import bundled_helper, install_launcher
    source = (bundled_helper(root) if root is not None else None) or bundled_helper()
    return [str(install_launcher(source)), '--launch']


def _launcher_command():
    from .self_update import LAUNCHER_NAME
    launcher = data_dir() / 'launcher' / LAUNCHER_NAME
    return [str(launcher), '--launch'] if launcher.is_file() else None


def _startup_environment():
    return {key: os.environ[key] for key in PROFILE_ENV if key in os.environ}


def startup(command, *, enable=True, start_now=True):
    """Create (or with enable=False remove) this user's login startup entry.

    `start_now` also starts the command immediately (explicit installs). A
    repair (`start_now=False`) runs inside an app that may have been opened
    from Finder/Explorer without the user's shell PATH or profile variables, so
    it only creates a missing entry and keeps an existing entry's environment.
    """
    from .self_update import app_environment, detached_options
    if any('\n' in value or '\r' in value or '\0' in value
           for value in list(command) + list(_startup_environment().values())):
        raise InstallError('Startup paths and profile variables cannot contain line breaks.')
    if sys.platform == 'darwin':
        destination = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
        domain = f'gui/{os.getuid()}'
        if not enable:
            subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
            destination.unlink(missing_ok=True)
            return
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        environment.update(_startup_environment())
        if not start_now and destination.is_file():
            try:
                existing = plistlib.loads(destination.read_bytes())
            except (OSError, ValueError):
                existing = {}
            if existing.get('ProgramArguments') == list(command):
                return
            if isinstance(existing.get('EnvironmentVariables'), dict):
                environment = {key: value for key, value in existing['EnvironmentVariables'].items()
                               if isinstance(key, str) and isinstance(value, str)}
        # The launcher starts the app through LaunchServices and exits. Its own
        # rare output goes to a separate small file, never into agent.log.
        output = str(default_state_dir() / 'launcher-output.log')
        config = dict(Label=LABEL, ProgramArguments=list(command), RunAtLoad=True,
                      WorkingDirectory=str(data_dir()), ThrottleInterval=30, ProcessType='Interactive',
                      EnvironmentVariables=environment, StandardOutPath=output, StandardErrorPath=output)
        default_state_dir().mkdir(parents=True, exist_ok=True)
        _write_file(destination, plistlib.dumps(config))
        if start_now:
            subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
            result = subprocess.run(['launchctl', 'bootstrap', domain, str(destination)], capture_output=True)
            if result.returncode:
                raise InstallError('macOS did not accept the login item (launchctl bootstrap failed). '
                                   'Log out and back in, then run the installer again.')
    elif sys.platform == 'win32':
        import winreg
        appdata = os.environ.get('APPDATA')
        if appdata:
            # The former VBScript startup file: deprecated by Windows and
            # frequently flagged by antivirus. Only this exact file is ours.
            legacy = Path(appdata) / 'Microsoft/Windows/Start Menu/Programs/Startup/Sweetmeter.vbs'
            try:
                legacy.unlink(missing_ok=True)
            except OSError:
                pass
        if not enable:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
            return
        line = subprocess.list2cmdline(command)
        environment_file = data_dir() / 'startup-environment.json'
        if not start_now:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    if winreg.QueryValueEx(key, RUN_VALUE)[0] == line:
                        return
            except OSError:
                pass
        if start_now or not environment_file.exists():
            # A Run value cannot set environment variables; the launcher reads them.
            _write_file(environment_file, (json.dumps(_startup_environment(), sort_keys=True) + '\n').encode('utf-8'))
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, line)
        if start_now:
            subprocess.Popen(list(command), env=app_environment(), **detached_options())
    else:
        destination = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart/sweetmeter.desktop'
        if not enable:
            destination.unlink(missing_ok=True)
            return
        if not start_now and destination.is_file():
            return  # Keep the recorded profile variables; the launcher path is stable.
        # Desktop-entry quoting differs from shell quoting; escape reserved syntax.
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
        assignments = [f'{key}={value}' for key, value in _startup_environment().items()]
        desktop_command = ['env', *assignments, *command] if assignments else list(command)
        _write_file(destination, ('[Desktop Entry]\nType=Application\nName=Sweetmeter\n'
                                  'Exec=' + ' '.join(map(quote, desktop_command)) +
                                  '\nTerminal=false\nX-GNOME-Autostart-enabled=true\n').encode('utf-8'))
        if start_now:
            subprocess.Popen(list(command), env=app_environment(), **detached_options())


def startup_registered():
    if sys.platform == 'darwin':
        return (Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')).is_file()
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except OSError:
            return False
    return (Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart/sweetmeter.desktop').is_file()


def register_uninstall_entry(root, launcher):
    """Windows Settings > Apps entry (per-user, HKCU); no-op elsewhere."""
    if sys.platform != 'win32':
        return
    import winreg
    from .version import get_version
    executable = app_command(root)[0]
    values = {
        'DisplayName': 'Sweetmeter', 'DisplayVersion': get_version(), 'Publisher': 'Sweetmeter',
        'InstallLocation': str(root), 'DisplayIcon': executable,
        'UninstallString': subprocess.list2cmdline([str(launcher), '--uninstall', '--interactive']),
        'QuietUninstallString': subprocess.list2cmdline([str(launcher), '--uninstall']),
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ('NoModify', 'NoRepair'):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)


def _remove_uninstall_entry():
    if sys.platform != 'win32':
        return
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def ensure_registration(root, *, start_at_login=None, start_now=False, refresh_launcher=True):
    """Idempotently ensure install record, launcher and startup entry exist.

    Never installs dependencies or copies the application. `start_at_login`
    None keeps the user's recorded choice (default on).
    """
    root = Path(root).absolute()
    record = read_install_record()
    if start_at_login is None:
        start_at_login = record.get('startup', True) is not False
    wanted = {'kind': 'native', 'root': str(root), 'startup': bool(start_at_login)}
    data_dir().mkdir(parents=True, exist_ok=True)
    default_state_dir().mkdir(parents=True, exist_ok=True)
    if {key: record.get(key) for key in wanted} != wanted:
        _write_file(data_dir() / 'install.json', (json.dumps(wanted) + '\n').encode('utf-8'))
    command = None if refresh_launcher else _launcher_command()
    if command is None:
        command = native_startup_command(root)
    if start_at_login:
        retire_legacy_startup()
        startup(command + ['--background', '--state-dir', str(default_state_dir())], start_now=start_now)
    register_uninstall_entry(root, command[0])
    return command


def open_installed(destination=None):
    """Bring the managed installation forward without replacing it."""
    from .self_update import start_app
    destination = Path(destination or install_root()).absolute()
    if not _is_managed(read_install_record(), destination):
        raise ValueError('Existing installation is not managed by Sweetmeter.')
    if not Path(app_command(destination)[0]).is_file():
        raise ValueError('Existing Sweetmeter installation is incomplete.')
    state = default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / 'show-window').touch()
    start_app(destination)
    return destination


def _destination_for(source):
    """Where `source` should live: the intact recorded copy, else adopt, else default."""
    record = read_install_record()
    recorded = install_root().absolute()
    if _is_managed(record, recorded) and Path(app_command(recorded)[0]).is_file():
        return recorded
    if source in candidate_install_roots():
        return source  # Already in an Applications folder: adopt it, do not duplicate it.
    return recorded if recorded.exists() else default_install_root().absolute()


def install_native(application, *, start_at_login=True):
    from .self_update import strip_download_marks, validate_tree
    source = Path(application).absolute()
    expected = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
    if source.name != expected or not Path(app_command(source)[0]).is_file():
        raise ValueError('Choose the extracted native Sweetmeter application folder.')
    with install_lock():
        destination = _destination_for(source)
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError('Refusing symlinked installation path.')
        if destination.exists():
            record = read_install_record()
            if (destination != source and not _is_managed(record, destination)
                    and not looks_like_sweetmeter(destination)):
                raise ValueError('Existing installation is not managed by Sweetmeter.')
            if not Path(app_command(destination)[0]).is_file():
                raise ValueError('Existing Sweetmeter installation is incomplete.')
        else:
            validate_tree(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Never leave a half-copied application at the managed destination.
            with tempfile.TemporaryDirectory(prefix='.sweetmeter-install-', dir=destination.parent) as temporary:
                staged = Path(temporary) / expected
                shutil.copytree(source, staged, symlinks=True)
                # Verified by the installer/manifest: drop Windows download marks
                # so the installed app and launcher do not warn at every login.
                strip_download_marks(staged)
                staged.rename(destination)
        state = default_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        (state / 'show-window').touch()
        ensure_registration(destination, start_at_login=start_at_login, start_now=start_at_login)
        if not start_at_login:
            open_installed(destination)
    return destination


def install_current(*, start_at_login=True):
    if not getattr(sys, 'frozen', False):
        raise ValueError('Source installations use scripts/install_agent.py.')
    executable = Path(sys.executable).absolute()
    application = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    return install_native(application, start_at_login=start_at_login)


def repair_running_installation():
    """Called by the running managed app: restore missing registration quietly.

    Covers a crash between copying and registering, and a manually copied app.
    The launcher is only refreshed when no update is waiting for confirmation.
    """
    if not getattr(sys, 'frozen', False):
        return False
    executable = Path(sys.executable).absolute()
    application = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    root = install_root().absolute()
    if application != root or not looks_like_sweetmeter(root):
        return False
    updating = bool(os.environ.get('SWEETMETER_UPDATE_HEALTH')) or (data_dir() / 'companion-swap.json').exists()
    with install_lock(timeout=5):
        ensure_registration(root, start_now=False, refresh_launcher=not updating)
    return True


def stop_running_app(timeout=15):
    """Stop this user's running companion; True when none is running."""
    from .instance_lock import InstanceLock
    from .self_update import terminate_pid
    state = default_state_dir()
    lock_path = state / 'meter.lock'
    if not lock_path.exists():
        return True
    def free():
        try:
            InstanceLock(lock_path).close()
            return True
        except OSError:
            return False
    if free():
        return True
    try:
        pid = int((state / 'meter.pid').read_text(encoding='ascii').strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid != os.getpid():
        terminate_pid(pid, timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if free():
            return True
        time.sleep(.2)
    return False


def _managed_roots():
    record = read_install_record()
    roots = []
    if record.get('kind') == 'native' and isinstance(record.get('root'), str):
        root = Path(record['root']).absolute()
        if root in candidate_install_roots():
            roots.append(root)
    default = default_install_root().absolute()
    if default not in roots and looks_like_sweetmeter(default):
        roots.append(default)  # Copied but never recorded (interrupted install).
    return roots


def _remove(path, removed):
    from .self_update import _retry
    path = Path(path)
    if path.is_symlink() or path.is_file():
        _retry(lambda: path.unlink(missing_ok=True))
        removed.append(path)
    elif path.is_dir():
        _retry(lambda: shutil.rmtree(path))
        removed.append(path)


def uninstall(remove_data=False, *, wait_pid=None):
    """Remove Sweetmeter for this user; keeps settings/state unless `remove_data`.

    Removes the startup entry, the managed app (only recorded/recognized
    locations), the launcher, the install record and, on Windows, the Apps
    entry. Returns the removed paths.
    """
    from .self_update import LAUNCHER_NAME, _alive
    removed = []
    with install_lock():
        if wait_pid:
            deadline = time.monotonic() + 30
            while _alive(wait_pid) and time.monotonic() < deadline:
                time.sleep(.2)
        if not stop_running_app():
            raise InstallError('Sweetmeter is still running. Quit it, then run the uninstaller again.')
        startup([], enable=False)
        record = read_install_record()
        for root in _managed_roots():
            if any(p.is_symlink() for p in (root, *root.parents)):
                continue
            for sibling in root.parent.glob(root.name + '.*'):
                if re.fullmatch(re.escape(root.name) + r'\.(incoming|previous)-[0-9a-f]{12}', sibling.name):
                    _remove(sibling, removed)
            _remove(root, removed)
        runtime = data_dir() / 'runtime'
        if record.get('kind') == 'source' and record.get('root') == str(runtime):
            _remove(runtime, removed)
        launcher = data_dir() / 'launcher'
        for name in (LAUNCHER_NAME, LAUNCHER_NAME + '.new', LAUNCHER_NAME + '.old'):
            try:
                _remove(launcher / name, removed)
            except OSError:
                pass  # A running Windows launcher; the folder is harmless.
        try:
            launcher.rmdir()
        except OSError:
            pass
        _remove_uninstall_entry()
        for name in MANAGED_FILES:
            _remove(data_dir() / name, removed)
        if remove_data:
            _remove(default_state_dir(), removed)
            _remove(data_dir() / 'retired-startup', removed)
            _remove(data_dir() / 'companion-update.lock', removed)
    if remove_data:
        try:
            (data_dir() / 'install.lock').unlink(missing_ok=True)
            data_dir().rmdir()
            removed.append(data_dir())
        except OSError:
            pass
    return removed


def _running_inside_removed_tree():
    executable = Path(sys.executable).absolute()
    return any(executable.is_relative_to(path) for path in (*_managed_roots(), data_dir()))


def request_uninstall(remove_data=False):
    """For the running app (e.g. a GUI menu item): hand removal to a helper
    process outside the application, then the caller must quit promptly."""
    from .self_update import HELPER_NAME, app_environment, bundled_helper, detached_options
    if getattr(sys, 'frozen', False):
        source = bundled_helper()
        if source is None:
            raise InstallError('The uninstaller is missing from this Sweetmeter package.')
        folder = Path(tempfile.mkdtemp(prefix='sweetmeter-uninstall-'))
        helper = folder / HELPER_NAME
        shutil.copyfile(source, helper)
        helper.chmod(0o700)
        command = [str(helper), '--uninstall']
        cwd = str(folder)
    else:
        command = [sys.executable, '-m', 'meter', '--uninstall']
        cwd = str(Path(__file__).resolve().parents[1])
    command += ['--wait-pid', str(os.getpid())] + (['--remove-data'] if remove_data else [])
    subprocess.Popen(command, cwd=cwd, env=app_environment(), **detached_options())


def _message_box(text, flags=0x40):
    if sys.platform != 'win32':
        print(text)
        return 0
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, text, 'Sweetmeter', flags)


def uninstall_main(argv):
    """`--uninstall [--remove-data] [--wait-pid PID] [--interactive]` for app and launcher."""
    import argparse
    from .self_update import HELPER_NAME, LAUNCHER_NAME, app_environment, detached_options, helper_log
    parser = argparse.ArgumentParser(prog='Sweetmeter --uninstall')
    parser.add_argument('--remove-data', action='store_true')
    parser.add_argument('--wait-pid', type=int)
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--interactive-done', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    notify = args.interactive or args.interactive_done
    if args.interactive and sys.platform == 'win32':
        answer = _message_box('Remove Sweetmeter from this computer?\n\n'
                              'Yes: also delete its settings, device pairing and usage cache.\n'
                              'No: keep them for a later reinstall.', 0x3 | 0x20)
        if answer == 2:
            return 1
        args.remove_data = answer == 6
    if sys.platform == 'win32' and getattr(sys, 'frozen', False) and _running_inside_removed_tree():
        # Windows cannot delete a running executable: continue from a copy.
        if Path(sys.executable).name in (HELPER_NAME, LAUNCHER_NAME):
            folder = Path(tempfile.mkdtemp(prefix='sweetmeter-uninstall-'))
            copy = folder / HELPER_NAME
            shutil.copyfile(sys.executable, copy)
            command = [str(copy), '--uninstall', '--wait-pid', str(os.getpid())]
            command += (['--remove-data'] if args.remove_data else [])
            command += (['--interactive-done'] if args.interactive else [])
            subprocess.Popen(command, cwd=str(folder), env=app_environment(), **detached_options())
        else:
            request_uninstall(args.remove_data)
        print('Sweetmeter is being removed in the background.')
        return 0
    try:
        removed = uninstall(args.remove_data, wait_pid=args.wait_pid)
    except (InstallError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, InstallError) else 'Uninstall failed: ' + type(error).__name__ + ': ' + str(error)
        helper_log(message)
        if notify:
            _message_box(message, 0x10)
        else:
            print(message, file=sys.stderr)
        return 1
    message = ('Sweetmeter was removed.' + ('' if args.remove_data else
               ' Settings and device pairing were kept in ' + str(default_state_dir()) + '.'))
    if notify:
        _message_box(message)
    else:
        print(message)
        for path in removed:
            print('  removed', path)
    return 0
