"""Verified companion replacement with an external helper and health rollback.

Caller must obtain artifact from the verified signed release manifest. This
module repeats size/hash validation and never downloads or installs on discovery.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from .paths import app_command, compatible_platforms, data_dir, install_root, resource_root

MAX_UNPACKED = 1024 * 1024 * 1024
MAX_FILES = 20000
HELPER_NAME = 'sweetmeter-update-helper' + ('.exe' if sys.platform == 'win32' else '')
LAUNCHER_NAME = 'sweetmeter-launcher' + ('.exe' if sys.platform == 'win32' else '')
# Windows: never give a background process a console window.
CREATE_NO_WINDOW = 0x08000000
UPDATE_ENVIRONMENT = ('SWEETMETER_UPDATE_HEALTH', 'SWEETMETER_UPDATE_NONCE', 'SWEETMETER_UPDATE_BLUETOOTH',
                      'SWEETMETER_UPDATE_DEADLINE')
# Forwarded explicitly on macOS, where LaunchServices does not inherit our environment.
FORWARDED_ENVIRONMENT = ('PATH', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'SWEETMETER_CODEX_PATH',
                         'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'PYINSTALLER_RESET_ENVIRONMENT',
                         *UPDATE_ENVIRONMENT)
# Without a deadline from the helper (older helpers allow 150 s after start).
BLUETOOTH_HEALTH_TIMEOUT = 110
# Time the helper gives an updated app to confirm its health, and its cap. It
# covers a macOS Bluetooth permission prompt the user answers slowly.
HEALTH_TIMEOUT = 180
HEALTH_TIMEOUT_CAP = 300
HELPER_LOG_LIMIT = 256 * 1024


def helper_log(message):
    """Size-bounded diagnostics for windowless helper/launcher processes."""
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'helper.log'
        if path.exists() and path.stat().st_size > HELPER_LOG_LIMIT:
            os.replace(path, path.with_name('helper.log.1'))
        with path.open('a', encoding='utf-8') as output:
            output.write(time.strftime('%Y-%m-%dT%H:%M:%S ') + str(message).replace('\n', ' ')[:2000] + '\n')
    except OSError:
        pass


def detached_options():
    """Popen options for a background process that outlives its parent and has no window."""
    if sys.platform == 'win32':
        return dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dict(start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def app_environment(extra=None):
    environment = os.environ.copy()
    for key in UPDATE_ENVIRONMENT:
        environment.pop(key, None)
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
        # A one-file launcher points LD_LIBRARY_PATH at its own temporary
        # libraries; the app must load its own (PyInstaller keeps the original).
        original = environment.pop('LD_LIBRARY_PATH_ORIG', None)
        if original is not None:
            environment['LD_LIBRARY_PATH'] = original
        else:
            environment.pop('LD_LIBRARY_PATH', None)
    environment.update(extra or {})
    return environment


def start_app(root, arguments=(), *, extra_env=None, background=False, wait=False):
    """Start the managed app so the OS attributes its privacy permissions to it.

    On macOS a child of a bare executable (launcher, helper, Terminal) has that
    parent as its TCC "responsible process", so Bluetooth permission would be
    asked for, and recorded against, the wrong program. LaunchServices (`open`)
    starts the bundle as its own responsible process. `wait` keeps `open`
    alive until the app exits, so `poll()` still reports an early exit.
    """
    root = Path(root)
    environment = app_environment(extra_env)
    if sys.platform == 'darwin':
        command = ['/usr/bin/open', '-n']
        if background:
            command.append('-g')
        if wait:
            command.append('-W')
        for key in FORWARDED_ENVIRONMENT:
            if key in environment:
                command += ['--env', key + '=' + environment[key]]
        command += ['-a', str(root), '--args', *arguments]
    else:
        command = app_command(root) + list(arguments)
    return subprocess.Popen(command, env=environment, cwd=str(root.parent), **detached_options())


def _retry(operation, *, attempts=8, delay=.25):
    """Retry filesystem operations that Windows can refuse while a scanner holds a file."""
    for attempt in range(attempts):
        try:
            return operation()
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(delay * (2 ** attempt), 8))


def _rmtree(path):
    if Path(path).exists():
        _retry(lambda: shutil.rmtree(path))


def strip_download_marks(root):
    """Remove Windows Mark-of-the-Web streams from a verified, copied tree."""
    if sys.platform != 'win32':
        return
    root = Path(root)
    for item in [root, *root.rglob('*')]:
        if item.is_file() and not item.is_symlink():
            try:
                os.remove(str(item) + ':Zone.Identifier')
            except OSError:
                pass


def bundled_helper(root=None):
    """The update helper shipped inside an application root (or this running app)."""
    if root is None:
        candidates = [resource_root()]
        top = resource_root()
    else:
        top = Path(root).absolute()
        candidates = [top / 'Contents/Frameworks', top / 'Contents/Resources', top / 'Contents/MacOS',
                      top / '_internal', top]
    for folder in candidates:
        path = folder / HELPER_NAME
        try:
            if path.is_file() and (root is None or path.resolve().is_relative_to(top.resolve())):
                return path
        except OSError:
            continue
    return None


def _file_digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def install_launcher(source=None):
    """Copy the stable recovery launcher outside the app; atomic and idempotent.

    Returns the launcher path. A running Windows executable cannot be replaced
    but can be renamed, so an in-use launcher is moved aside first.
    """
    source = Path(source) if source is not None else bundled_helper()
    if source is None or not source.is_file():
        raise RuntimeError('Recovery launcher is missing from this Sweetmeter package')
    folder = data_dir() / 'launcher'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LAUNCHER_NAME
    old = folder / (LAUNCHER_NAME + '.old')
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    if target.is_file() and not target.is_symlink() and _file_digest(target) == _file_digest(source):
        return target
    temporary = folder / (LAUNCHER_NAME + '.new')
    temporary.unlink(missing_ok=True)
    shutil.copyfile(source, temporary)
    temporary.chmod(0o700)
    with temporary.open('r+b') as handle:
        os.fsync(handle.fileno())
    strip_download_marks(temporary)
    try:
        os.replace(temporary, target)
    except PermissionError:
        if sys.platform != 'win32':
            raise
        _retry(lambda: os.replace(target, old))
        os.replace(temporary, target)
    _sync_dir(folder)
    return target


def _json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    result = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(result, dict):
        raise ValueError('Expected JSON object')
    return result


def _write(path, value):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(json.dumps(value, sort_keys=True) + '\n')
            output.flush()
            os.fsync(output.fileno())
        _retry(lambda: tmp.replace(path), attempts=5)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(path.parent)


def _sync_dir(path):
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _journal_paths(record):
    root = _safe_path(Path(record['root']))
    if (type(record.get('schema')) is not int or record.get('schema') != 1
            or root != install_root().absolute()
            or record.get('stage') not in {'prepared', 'old_moved', 'new_installed', 'confirmed'}
            or not isinstance(record.get('nonce'), str)
            or not re.fullmatch('[0-9a-f]{6,64}', record.get('nonce', ''))):
        raise ValueError('Invalid update recovery journal')
    suffix = record['nonce'][:12]
    return root, root.with_name(root.name + '.incoming-' + suffix), root.with_name(root.name + '.previous-' + suffix)


@contextmanager
def _update_lock():
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'companion-update.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a+b') as handle:
        try:
            if sys.platform == 'win32':
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('A companion update or recovery is already running') from error
        try:
            yield
        finally:
            if sys.platform == 'win32':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def update_in_progress():
    """True while a helper or launcher holds the update/recovery lock."""
    try:
        with _update_lock():
            return (data_dir() / 'companion-swap.json').exists()
    except RuntimeError:
        return True


def _serialized(function):
    @wraps(function)
    def run(*args, **kwargs):
        with _update_lock():
            return function(*args, **kwargs)
    return run


@_serialized
def recover_update():
    """Stable external launcher calls this before the managed app can start."""
    return recover_update_locked()


def recover_update_locked():
    """recover_update for a caller that already holds `_update_lock()`."""
    journal = data_dir() / 'companion-swap.json'
    if not journal.is_file():
        return False
    if journal.stat().st_size > 16384:
        raise ValueError('Oversized recovery journal')
    record = _json_object(journal.read_text())
    root, incoming, backup = _journal_paths(record)
    # A helper can die while its new app survives. Never replace that running
    # app or its libraries; the next launcher can recover after the app exits.
    from .instance_lock import InstanceLock
    state = _safe_path(Path(record['state_dir']))
    state.mkdir(parents=True, exist_ok=True)
    try:
        instance = InstanceLock(state / 'meter.lock')
    except OSError as error:
        raise RuntimeError('Quit the running Sweetmeter before recovering its interrupted update') from error
    try:
        return _recover_stopped(record, root, incoming, backup, journal)
    finally:
        instance.close()


def _recover_stopped(record, root, incoming, backup, journal):
    if record.get('stage') == 'confirmed':
        if not root.is_dir():
            raise RuntimeError('Confirmed installation missing; manual recovery required')
        _rmtree(backup)
        _write(Path(record['state_dir']) / 'companion-update-result.json',
               {'status': 'success', 'version': record['version']})
    else:
        if backup.exists():
            if root.exists() and tree_pids(root):
                # The new app hung before taking its instance lock: never
                # delete a tree a process is still running from.
                raise RuntimeError('Quit the running Sweetmeter before recovering its interrupted update')
            _rmtree(root)
            _retry(lambda: backup.rename(root))
            _sync_dir(root.parent)
        elif not root.is_dir():
            raise RuntimeError('No installation or backup available for recovery')
        state = Path(record['state_dir'])
        state.mkdir(parents=True, exist_ok=True)
        _write(state / 'companion-update-result.json',
               {'status': 'rollback', 'version': record['version'],
                'reason': 'Recovered an interrupted or unconfirmed application update'})
    _rmtree(incoming)
    journal.unlink()
    _sync_dir(journal.parent)
    return True


def startup_environment():
    """Profile variables saved at install time (a Windows Run value cannot carry them)."""
    path = data_dir() / 'startup-environment.json'
    try:
        if path.stat().st_size > 65536:
            return {}
        values = _json_object(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    from .installation import PROFILE_ENV
    return {key: value for key, value in values.items()
            if key in PROFILE_ENV and isinstance(value, str) and '\0' not in value}


def _app_running(arguments):
    """True while a companion holds the instance lock of the state folder the
    launcher would start it with."""
    from .instance_lock import InstanceLock
    from .paths import default_state_dir
    arguments = list(arguments)
    state = default_state_dir()
    if '--state-dir' in arguments[:-1]:
        state = Path(arguments[arguments.index('--state-dir') + 1])
    lock = state / 'meter.lock'
    if not lock.is_file():
        return False
    try:
        InstanceLock(lock).close()
    except OSError:
        return True
    return False


def launch_installed(arguments=()):
    """Login launcher: finish or roll back an interrupted swap, then start the app."""
    try:
        recover_update()
    except RuntimeError as error:
        # The app is already running or an update is in progress; starting
        # another copy would only race it. Leave recovery to the next start.
        helper_log('Launcher did not start Sweetmeter: ' + str(error))
        return 0
    root = install_root()
    if not Path(app_command(root)[0]).is_file():
        helper_log('Launcher cannot find the installed app at ' + str(root))
        return 1
    if _app_running(arguments):
        # E.g. the login item was reloaded after a repair: never start a
        # second copy (it would only exit again).
        return 0
    extra = {key: value for key, value in startup_environment().items() if key not in os.environ}
    process = start_app(root, arguments, extra_env=extra, background='--background' in arguments)
    if sys.platform == 'win32':
        return 0  # Nothing supervises a Run-key process; stay out of the way.
    # macOS: `open` returns once LaunchServices has started the app. Linux:
    # an XDG autostart unit ends when its main process exits, so wait for the
    # app to keep its cgroup alive.
    return process.wait()


def _safe_path(path):
    path = Path(path).absolute()
    # Refuse symlinked install directories/parents, including dangling links.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked installation paths are not supported')
    return path


def _entry_path(name):
    path = PurePosixPath(name)
    if ('\\' in name or '\x00' in name or ':' in name or path.is_absolute()
            or any(part in ('..', '.', '') for part in name.rstrip('/').split('/'))):
        raise ValueError('Unsafe archive path')
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if (part.rstrip(' .') != part or stem in {'CON', 'PRN', 'AUX', 'NUL'}
                or stem in {f'COM{i}' for i in range(1, 10)}
                or stem in {f'LPT{i}' for i in range(1, 10)}):
            raise ValueError('Unsafe platform filename')
    return path


def _link_target(path, target):
    if not target or len(target) > 4096 or any(c in target for c in ('\\', '\x00', ':')):
        raise ValueError('Invalid symlink target')
    relative = PurePosixPath(target)
    if relative.is_absolute() or path.parts[0] != 'Sweetmeter.app':
        raise ValueError('Only internal macOS application links are allowed')
    parts = list(path.parent.parts)
    for part in relative.parts:
        if part == '..':
            if len(parts) <= 1:
                raise ValueError('Symlink escapes application root')
            parts.pop()
        elif part != '.':
            parts.append(part)
    if not parts or parts[0] != 'Sweetmeter.app':
        raise ValueError('Symlink escapes application root')
    return PurePosixPath(*parts)


def _validate_links(paths, links):
    directories = set()
    for path in paths:
        directories.update(path.parents)
        # No regular or link entry may be created through another symlink.
        if any(parent in links for parent in path.parents):
            raise ValueError('Archive writes through a symlink parent')
    for link, target in links.items():
        pending = _link_target(link, target)
        visited = set()
        for _ in range(64):
            if pending in visited:
                raise ValueError('Symlink cycle')
            visited.add(pending)
            prefix = next((p for p in [*reversed(pending.parents), pending] if p in links), None)
            if prefix is None:
                if pending not in paths and pending not in directories:
                    raise ValueError('Dangling internal symlink')
                break
            replaced = _link_target(prefix, links[prefix])
            pending = replaced / pending.relative_to(prefix)
        else:
            raise ValueError('Symlink chain exceeds depth limit')


def validate_tree(root):
    """Validate installed/staged native trees before preserving internal links."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Application root cannot be a symlink')
    paths, links = set(), {}
    names = set()
    for item in root.rglob('*'):
        relative = PurePosixPath(root.name) / item.relative_to(root).as_posix()
        _entry_path(str(relative))
        if str(relative).casefold() in names:
            raise ValueError('Case-colliding native application paths')
        names.add(str(relative).casefold())
        paths.add(relative)
        if item.is_symlink():
            links[relative] = os.readlink(item)
        elif not item.is_file() and not item.is_dir():
            raise ValueError('Special file in application tree')
    _validate_links(paths, links)


def _inspect_archive(archive):
    seen, total, paths, links = set(), 0, set(), {}
    entries = archive.infolist()
    if not entries or len(entries) > MAX_FILES:
        raise ValueError('Invalid archive file count')
    for item in entries:
        # On Windows ZipInfo normalizes backslashes in filename. Inspect the
        # original archive spelling so normalization cannot hide invalid input.
        path = _entry_path(item.orig_filename)
        canonical = str(path).casefold()
        if canonical in seen:
            raise ValueError('Duplicate/case-colliding archive entry')
        seen.add(canonical)
        paths.add(path)
        kind = stat.S_IFMT(item.external_attr >> 16)
        if kind == stat.S_IFLNK:
            if item.file_size > 4096:
                raise ValueError('Oversized symlink target')
            links[path] = archive.read(item).decode('utf-8', 'strict')
        elif kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError('Archive special files are forbidden')
        total += item.file_size
        if total > MAX_UNPACKED:
            raise ValueError('Unpacked archive exceeds size limit')
    _validate_links(paths, links)
    return entries, links


def extract_package(package, destination):
    """Extract regular files first, then bounded internal macOS links last."""
    with zipfile.ZipFile(package) as archive:
        entries, links = _inspect_archive(archive)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Extraction requires an empty destination')
        destination.mkdir(parents=True, exist_ok=True)
        for item in entries:
            path = PurePosixPath(item.filename)
            if path in links:
                continue
            target = destination.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
        for path, link in links.items():
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)


def _architecture(raw, os_name):
    if os_name == 'macos' and raw[:4] == b'\xcf\xfa\xed\xfe' and len(raw) >= 32:
        return {0x1000007: 'x86_64', 0x100000c: 'arm64'}.get(struct.unpack_from('<I', raw, 4)[0])
    if os_name == 'windows' and raw[:2] == b'MZ' and len(raw) >= 64:
        offset = struct.unpack_from('<I', raw, 60)[0]
        if offset + 6 <= len(raw) and raw[offset:offset + 4] == b'PE\0\0':
            return {0x8664: 'x86_64', 0xaa64: 'arm64'}.get(struct.unpack_from('<H', raw, offset + 4)[0])
    if os_name == 'linux' and raw[:6] == b'\x7fELF\x02\x01' and len(raw) >= 64:
        return {62: 'x86_64', 183: 'arm64'}.get(struct.unpack_from('<H', raw, 18)[0])
    return None


def inspect_package(path, *, os_name, arch, version, source_commit=None, source_tree=None):
    """Release-builder check: validated archive, native header and build receipt.

    Runtime staging separately authenticates the ZIP using the signed manifest;
    this adjacent build receipt is a CI provenance check, not an end-user key.
    """
    path = Path(path)
    sidecar_path = path.with_name(path.name + '.build.json')
    if sidecar_path.stat().st_size > 16384:
        raise ValueError('Oversized build provenance')
    receipt = _json_object(sidecar_path.read_text(encoding='utf-8'))
    with path.open('rb') as source:
        if receipt.get('artifact_sha256') != hashlib.file_digest(source, 'sha256').hexdigest():
            raise ValueError('Native artifact differs from its build receipt')
    with zipfile.ZipFile(path) as archive:
        entries, links = _inspect_archive(archive)
        expected_root = 'Sweetmeter.app' if os_name == 'macos' else 'Sweetmeter'
        if {PurePosixPath(item.filename).parts[0] for item in entries} != {expected_root}:
            raise ValueError('Unexpected native archive root')
        prefix = expected_root + ('/Contents/Resources/' if os_name == 'macos' else '/_internal/')
        metadata_name = prefix + 'build-metadata.json'
        metadata_entry = archive.getinfo(metadata_name)
        if (stat.S_IFMT(metadata_entry.external_attr >> 16) not in (0, stat.S_IFREG)
                or metadata_entry.is_dir() or metadata_entry.file_size > 16384):
            raise ValueError('Invalid embedded build metadata')
        metadata = _json_object(archive.read(metadata_name))
        expected_entry = (expected_root + '/Contents/MacOS/Sweetmeter' if os_name == 'macos'
                          else expected_root + ('/Sweetmeter.exe' if os_name == 'windows' else '/Sweetmeter'))
        if (type(metadata.get('schema')) is not int or metadata.get('schema') != 1
                or metadata.get('kind') != 'sweetmeter-companion-build'
                or metadata.get('version') != version or metadata.get('os') != os_name
                or metadata.get('arch') != arch or metadata.get('entrypoint') != expected_entry
                or metadata.get('dirty') is not False or metadata.get('test_build') is not False):
            raise ValueError('Native package provenance is not a clean matching release build')
        for field, expected in [('source_commit', source_commit), ('source_tree', source_tree)]:
            value = metadata.get(field, '')
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value) or (expected is not None and value != expected):
                raise ValueError('Native build source provenance mismatch: ' + field)
        fingerprint = metadata.get('source_fingerprint')
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise ValueError('Invalid native source fingerprint')
        if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
               for key, value in metadata.items()):
            raise ValueError('Embedded metadata differs from build receipt')
        entry = archive.getinfo(expected_entry)
        mode = entry.external_attr >> 16
        if (stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.is_dir()
                or (os_name in ('macos', 'linux') and not mode & 0o111)):
            raise ValueError('Native entrypoint must be a regular executable')
        raw = archive.read(expected_entry)
        if _architecture(raw, os_name) != arch:
            raise ValueError('Native executable architecture does not match release target')
        if hashlib.sha256(raw).hexdigest() != receipt.get('executable_sha256'):
            raise ValueError('Native executable differs from build receipt')
    return metadata


@dataclass
class StagedUpdate:
    supported: bool
    reason: str
    manual_path: Path
    plan_path: Path | None = None

    def launch(self):
        if not self.supported or self.plan_path is None:
            raise RuntimeError(self.reason)
        source = bundled_helper()
        if source is None:
            raise RuntimeError('Updater helper missing; use the verified manual package')
        helper = self.plan_path.parent / HELPER_NAME
        shutil.copyfile(source, helper)
        helper.chmod(0o700)
        options = detached_options()
        subprocess.Popen([str(helper), str(self.plan_path)], cwd=str(self.plan_path.parent), **options)
        # A one-file helper unpacks itself first; antivirus scanning can slow that.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (self.plan_path.parent / 'helper-ready').exists():
                return
            time.sleep(.1)
        raise RuntimeError('Update helper did not start; current app has not been replaced')


def package_matches_host(artifact):
    return (artifact.get('os'), artifact.get('arch')) in compatible_platforms()


def stage_update(package_path, artifact, state_dir, *, bluetooth_baseline=None):
    """Verify and unpack a release package; plan the swap for the external helper.

    `bluetooth_baseline` is the running app's Bluetooth health. Only when the
    current version demonstrably works (`'ok'`) does the new version have to
    prove Bluetooth access before the previous version is discarded.
    """
    package_path = Path(package_path)
    if (data_dir() / 'companion-swap.json').exists():
        raise RuntimeError('An earlier update needs recovery; start Sweetmeter using its installed launcher')
    if artifact.get('kind') != 'companion':
        raise ValueError('Not a companion package')
    if package_path.stat().st_size != artifact['size']:
        raise ValueError('Package size mismatch')
    with package_path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != artifact['sha256']:
        raise ValueError('Package checksum mismatch')
    if not package_matches_host(artifact):
        raise ValueError('Package targets another operating system or architecture')
    state_dir = _safe_path(Path(state_dir))
    updates = state_dir / 'updates'
    updates.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_path(updates)
    staging = Path(tempfile.mkdtemp(prefix='companion-', dir=updates))
    staging.chmod(0o700)
    extracted = staging / 'package'
    extracted.mkdir()
    try:
        extract_package(package_path, extracted)
        children = list(extracted.iterdir())
        expected = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
        if len(children) != 1 or children[0].name != expected or not children[0].is_dir():
            raise ValueError('Wrong native package structure')
        candidate = children[0]
        if not Path(app_command(candidate)[0]).is_file():
            raise ValueError('Package executable is missing')
        root = _safe_path(install_root())
        record = data_dir() / 'install.json'
        try:
            managed = json.loads(record.read_text())
        except (OSError, ValueError):
            managed = {}
        supported = (getattr(sys, 'frozen', False)
                     and managed.get('root') == str(root)
                     and managed.get('kind') == 'native'
                     and Path(sys.executable).resolve() == Path(app_command(root)[0]).resolve()
                     and os.access(root.parent, os.W_OK))
        if not supported:
            return StagedUpdate(False, 'Use the verified extracted package for manual installation; '
                                'automatic updates require a writable managed native installation.', candidate)
        plan = dict(schema=1, root=str(root), candidate=str(candidate), parent_pid=os.getpid(),
                    state_dir=str(state_dir), version=artifact['version'], nonce=secrets.token_hex(24),
                    health_timeout=HEALTH_TIMEOUT, data_dir=str(data_dir()),
                    bluetooth_baseline=bluetooth_baseline if isinstance(bluetooth_baseline, str) else None)
        plan_path = staging / 'plan.json'
        _write(plan_path, plan)
        return StagedUpdate(True, '', candidate, plan_path)
    except BaseException:
        _rmtree(staging)
        raise


def cleanup_staging(state_dir, *, keep=()):
    """Remove finished update staging folders; never one an update still needs."""
    updates = Path(state_dir) / 'updates'
    if not updates.is_dir():
        return []
    keep = {Path(path).absolute() for path in keep}
    removed = []
    try:
        with _update_lock():
            if (data_dir() / 'companion-swap.json').exists():
                return []  # The launcher still needs this swap's files.
            marker = os.environ.get('SWEETMETER_UPDATE_HEALTH')
            if marker:
                keep.add(Path(marker).absolute().parent)
            for folder in updates.glob('companion-*'):
                if folder.is_dir() and not folder.is_symlink() and folder.absolute() not in keep:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed.append(folder)
    except RuntimeError:
        return []  # A helper is running; its staging folder is in use.
    return removed


BLUETOOTH_ACCEPTED = ('ok', 'off')
# While macOS shows its Bluetooth permission prompt the radio reports no state
# (health None). After this long without one, the user is asked to answer it.
BLUETOOTH_PROMPT_NOTICE_AFTER = 10
# The watcher must answer before the helper gives up. A helper that passes
# SWEETMETER_UPDATE_DEADLINE says exactly when; older helpers allow 150 s from
# the app's start, so the watcher then keeps a safe margin below that.
BLUETOOTH_DEADLINE_MARGIN = 15


def _health_timeout(default):
    try:
        deadline = float(os.environ.get('SWEETMETER_UPDATE_DEADLINE', ''))
    except ValueError:
        return default
    remaining = deadline - time.time() - BLUETOOTH_DEADLINE_MARGIN
    return max(5.0, min(remaining, float(HEALTH_TIMEOUT_CAP)))


def confirm_update_health(version, *, radio=None, on_confirmed=None, notify=None,
                          timeout=None, interval=1.0, radio_failed=None):
    """Report startup health to the update helper without blocking the UI.

    Called after local startup checks passed. When the helper says the
    previous version had working Bluetooth, the receipt is written only after
    this version's radio also works: an ad-hoc re-signed macOS app can lose
    its Bluetooth permission, and keeping the backup lets the helper restore a
    version that works.

    `radio` is the radio or a callable returning the current one (the app
    replaces its radio when Bluetooth is restarted). Bluetooth that is on or
    switched off confirms. No state yet (None, e.g. while the macOS permission
    prompt is open) is waiting: the user is reminded after a few seconds, and
    if nothing is known by the deadline the update is kept. Only a denied
    permission ('unauthorized') still present at the deadline, or a radio that
    failed to start (`radio_failed()` / its startup_error), rolls back.
    Returns the watcher thread, if any.
    """
    marker = os.environ.get('SWEETMETER_UPDATE_HEALTH')
    nonce = os.environ.get('SWEETMETER_UPDATE_NONCE')
    if not (marker and nonce):
        if on_confirmed:
            on_confirmed()
        return None
    marker = Path(marker)
    folder = marker.parent
    identity = {'version': version, 'nonce': nonce, 'pid': os.getpid()}
    try:
        _write(folder / 'started.json', identity)
    except OSError:
        pass

    def confirm():
        _write(marker, identity)
        if on_confirmed:
            on_confirmed()

    if os.environ.get('SWEETMETER_UPDATE_BLUETOOTH') != 'ok' or radio is None:
        confirm()
        return None
    # A radio object has `health`; anything else callable returns the current radio.
    current = radio if callable(radio) and not hasattr(radio, 'health') else (lambda: radio)
    limit = _health_timeout(BLUETOOTH_HEALTH_TIMEOUT) if timeout is None else timeout

    def failed(device):
        if radio_failed is not None:
            try:
                return bool(radio_failed())
            except Exception:  # noqa: BLE001 - a probe must not kill the watcher
                return False
        return bool(getattr(device, 'startup_error', None))

    def watch():
        started = time.monotonic()
        deadline = started + limit
        health, broken, told = None, False, set()
        while time.monotonic() < deadline:
            device = current()
            health = getattr(device, 'health', None) if device is not None else None
            if health in BLUETOOTH_ACCEPTED:
                confirm()
                return
            broken = failed(device)
            if broken:
                break
            if notify and health == 'unauthorized' and 'unauthorized' not in told:
                told.add('unauthorized')
                notify('Allow Bluetooth for Sweetmeter to finish the update. If you do not, '
                       'the previous version is restored automatically.')
            elif (notify and health is None and not told
                  and time.monotonic() - started >= BLUETOOTH_PROMPT_NOTICE_AFTER):
                told.add('waiting')
                notify('Allow Bluetooth for Sweetmeter to finish the update. If macOS or '
                       'Windows asks for Bluetooth access, choose Allow.')
            time.sleep(interval)
        if broken:
            reason = 'Bluetooth did not start in the updated app. The previous version was kept.'
        elif health == 'unauthorized':
            reason = ('Bluetooth permission is not available to the updated app. The previous '
                      'version was kept. Allow Sweetmeter in System Settings > Privacy & Security > '
                      'Bluetooth, then install the update again.')
        else:
            # Still waiting for an answer (or a transient adapter problem):
            # nothing proves the new version is broken, so it is kept.
            logging.info('Update kept: Bluetooth state still %s at the health deadline',
                         health or 'unknown')
            confirm()
            return
        logging.error('Update health check failed: %s', reason)
        _write(folder / 'unhealthy.json', dict(identity, reason=reason,
                                               bluetooth=health or ('failed' if broken else 'unknown')))

    thread = threading.Thread(target=watch, name='sweetmeter-update-health', daemon=True)
    thread.start()
    return thread


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_pid(pid, timeout=10):
    """Ask a process to stop, then force it; True once it is gone."""
    import signal
    if not _alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows.
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(.1)
    if sys.platform != 'win32':
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(.2)
    return not _alive(pid)


def _receipt(path, plan):
    try:
        if path.stat().st_size > 16384:
            return {}
        value = _json_object(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if value.get('nonce') != plan['nonce'] or type(value.get('pid')) is not int:
        return {}
    return value


def _refresh_launcher(root):
    """Once the new version is confirmed, its recovery launcher replaces the old one."""
    if not (data_dir() / 'launcher').is_dir():
        return
    source = bundled_helper(root)
    if source is None:
        helper_log('Updated app has no bundled launcher; keeping the existing launcher')
        return
    try:
        install_launcher(source)
    except OSError as error:
        helper_log('Launcher refresh failed: ' + type(error).__name__)


def tree_pids(root):
    """PIDs of processes whose executable lives inside `root` (never this one).

    Used so a rollback or uninstall never deletes a tree that a started app,
    even one that hangs before reporting its PID, is still running from."""
    root = Path(root)
    prefixes = {str(root.absolute()) + os.sep}
    try:
        prefixes.add(str(root.resolve()) + os.sep)
    except OSError:
        pass
    pids = set()
    if sys.platform == 'win32':
        return pids  # Windows refuses to rename or delete a running executable.
    if sys.platform.startswith('linux'):
        try:
            entries = list(Path('/proc').iterdir())
        except OSError:
            return pids
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                executable = os.readlink(entry / 'exe')
            except OSError:
                continue
            if any(executable.startswith(prefix) for prefix in prefixes):
                pids.add(int(entry.name))
    else:
        try:
            result = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,comm='], capture_output=True,
                                    text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return pids
        for line in str(result.stdout or '').splitlines():
            number, _, command = line.strip().partition(' ')
            if number.isdigit() and any(command.strip().startswith(prefix) for prefix in prefixes):
                pids.add(int(number))
    pids.discard(os.getpid())
    return pids


class TreeSwap:
    """Journaled replacement of the managed application tree.

    The same durable stages as a companion update (`prepared`, `old_moved`,
    `new_installed`, `confirmed`) are recorded in companion-swap.json, so the
    login launcher (`recover_update`) finishes or rolls back an interrupted
    swap. The caller must hold `_update_lock()` and must have stopped the app.
    """

    def __init__(self, root, *, nonce, state_dir, version):
        self.root = Path(root)
        self.incoming = self.root.with_name(self.root.name + '.incoming-' + nonce[:12])
        self.backup = self.root.with_name(self.root.name + '.previous-' + nonce[:12])
        self.journal = data_dir() / 'companion-swap.json'
        self.recovery = dict(schema=1, root=str(self.root), nonce=nonce, state_dir=str(state_dir),
                             version=version, stage='prepared')
        self.moved = self.replaced = self.journaled = False

    def prepare(self, candidate, *, strip_marks=False):
        """Copy a validated candidate next to the root (same filesystem)."""
        if self.incoming.exists() or self.backup.exists():
            raise ValueError('Replacement paths already exist')
        if self.journal.exists():
            raise RuntimeError('Another update recovery journal is already present')
        validate_tree(candidate)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate, self.incoming, symlinks=True)
        if strip_marks:
            strip_download_marks(self.incoming)
        for item in self.incoming.rglob('*'):
            if item.is_file() and not item.is_symlink():
                # Windows CRT _commit (os.fsync) requires a writable handle.
                # r+b preserves the bytes while allowing durable flush there.
                with item.open('r+b') as source:
                    os.fsync(source.fileno())

    def _stage(self, stage):
        self.recovery['stage'] = stage
        _write(self.journal, self.recovery)

    def swap(self):
        _write(self.journal, self.recovery)
        self.journaled = True
        # Antivirus, indexers or Explorer can briefly lock a file in the old app.
        _retry(lambda: self.root.rename(self.backup))
        self.moved = True
        _sync_dir(self.root.parent)
        self._stage('old_moved')
        _retry(lambda: self.incoming.rename(self.root))
        _sync_dir(self.root.parent)
        self.replaced = True
        self._stage('new_installed')

    def confirm(self):
        self._stage('confirmed')
        try:
            _rmtree(self.backup)
        except OSError:
            pass  # The launcher finishes cleanup at the next start.
        if not self.backup.exists():
            self.journal.unlink()
            _sync_dir(self.journal.parent)

    def rollback(self):
        """Put the previous tree back; True when it is at the root again.
        Never deletes a new tree that still has a running process."""
        if self.replaced and tree_pids(self.root):
            helper_log('Updated app is still running; the previous version is restored at the next login')
            return False
        restored = _restore_previous(self.root, self.backup, self.moved, self.replaced)
        if self.journaled and restored:
            self.journal.unlink(missing_ok=True)
            _sync_dir(self.journal.parent)
        return restored

    def cleanup(self):
        try:
            _rmtree(self.incoming)
        except OSError:
            pass


@_serialized
def apply_update(plan_path):
    """Detached helper entry; replace only the managed per-user install target."""
    plan_path = _safe_path(Path(plan_path))
    plan = json.loads(plan_path.read_text())
    root = _safe_path(Path(plan['root']))
    if plan.get('schema') != 1 or root != install_root().absolute():
        raise ValueError('Unrecognized managed installation')
    candidate = _safe_path(Path(plan['candidate']))
    if candidate.parent != plan_path.parent / 'package' or candidate.name != root.name:
        raise ValueError('Candidate is outside its private staging directory')
    record = json.loads((data_dir() / 'install.json').read_text())
    if record.get('root') != str(root) or record.get('kind') != 'native':
        raise ValueError('Installation is no longer managed')
    _write(plan_path.parent / 'helper-ready', {'pid': os.getpid()})
    deadline = time.monotonic() + 60
    while _alive(plan['parent_pid']):
        if time.monotonic() > deadline:
            raise RuntimeError('Current app did not exit; installation unchanged')
        time.sleep(.2)
    # From here on the previous app has exited: every failure must restart it.
    # Copy before renaming so replacement stays on the same filesystem.
    health = plan_path.parent / 'healthy.json'
    unhealthy = plan_path.parent / 'unhealthy.json'
    started = plan_path.parent / 'started.json'
    outcome = Path(plan['state_dir']) / 'companion-update-result.json'
    swap = TreeSwap(root, nonce=plan['nonce'], state_dir=plan['state_dir'], version=plan['version'])
    process = None
    try:
        swap.prepare(candidate)
        swap.swap()
        wait = min(HEALTH_TIMEOUT_CAP, max(10, int(plan['health_timeout'])))
        extra = {'SWEETMETER_UPDATE_HEALTH': str(health), 'SWEETMETER_UPDATE_NONCE': plan['nonce'],
                 # Wall-clock time by which the app must have answered.
                 'SWEETMETER_UPDATE_DEADLINE': '%.3f' % (time.time() + wait)}
        if isinstance(plan.get('bluetooth_baseline'), str):
            extra['SWEETMETER_UPDATE_BLUETOOTH'] = plan['bluetooth_baseline']
        process = start_app(root, ['--state-dir', plan['state_dir']], extra_env=extra, wait=True)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            value = _receipt(health, plan)
            running = process.poll() is None
            same = value.get('pid') == process.pid if sys.platform != 'darwin' else _alive(value.get('pid'))
            if value.get('version') == plan['version'] and same and running:
                swap.confirm()
                _write(outcome, {'status': 'success', 'version': plan['version']})
                _refresh_launcher(root)
                return
            failure = _receipt(unhealthy, plan)
            if failure:
                raise RuntimeError(str(failure.get('reason', 'Updated app reported a failed health check'))[:500])
            if not running:
                raise RuntimeError('Updated app exited before confirming startup')
            time.sleep(.2)
        raise RuntimeError('Updated app did not confirm startup health')
    except BaseException as error:
        _stop_candidate(process, [started, health, unhealthy], plan, root if swap.replaced else None)
        restored = swap.rollback()
        if restored and Path(app_command(root)[0]).is_file():
            try:
                start_app(root, ['--state-dir', plan['state_dir']])
            except OSError as launch_error:
                helper_log('Could not restart the previous app: ' + type(launch_error).__name__)
        reason = str(error) if restored else (str(error) + '. Recovery finishes at the next login.')
        _write(outcome, {'status': 'rollback', 'version': plan['version'], 'reason': reason[:600]})
        raise
    finally:
        swap.cleanup()


def _stop_candidate(process, receipts, plan, root=None):
    """Stop the updated app: the PIDs it reported, the started process and,
    because a macOS app started through `open` has another PID and can hang
    before reporting it, every process running from the new tree."""
    pids = {value['pid'] for value in (_receipt(path, plan) for path in receipts) if value}
    if process is not None and sys.platform != 'darwin':
        pids.add(process.pid)
    if process is not None and root is not None:
        pids |= tree_pids(root)
    for pid in pids:
        if pid != os.getpid():
            terminate_pid(pid)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _restore_previous(root, backup, moved, replaced):
    """Put the previous app back at `root`; True when it is there again."""
    try:
        if moved:
            if replaced:
                _rmtree(root)
            _retry(lambda: backup.rename(root))
            _sync_dir(root.parent)
        return root.is_dir()
    except OSError as error:
        helper_log('Restoring the previous app failed: ' + type(error).__name__)
        return False
