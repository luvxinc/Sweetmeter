"""Verified companion replacement with an external helper and health rollback.

Caller must obtain artifact from the verified signed release manifest. This
module repeats size/hash validation and never downloads or installs on discovery.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
import platform
import re
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

from .paths import app_command, data_dir, install_root, resource_root

MAX_UNPACKED = 1024 * 1024 * 1024
MAX_FILES = 20000


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
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as output:
        output.write(json.dumps(value, sort_keys=True) + '\n')
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(path)
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


def _serialized(function):
    @wraps(function)
    def run(*args, **kwargs):
        with _update_lock():
            return function(*args, **kwargs)
    return run


@_serialized
def recover_update():
    """Stable external launcher calls this before the managed app can start."""
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
        if backup.exists():
            shutil.rmtree(backup)
        _write(Path(record['state_dir']) / 'companion-update-result.json',
               {'status': 'success', 'version': record['version']})
    else:
        if backup.exists():
            if root.exists():
                shutil.rmtree(root)
            backup.rename(root)
            _sync_dir(root.parent)
        elif not root.is_dir():
            raise RuntimeError('No installation or backup available for recovery')
        state = Path(record['state_dir'])
        state.mkdir(parents=True, exist_ok=True)
        _write(state / 'companion-update-result.json',
               {'status': 'rollback', 'version': record['version'],
                'reason': 'Recovered an interrupted or unconfirmed application update'})
    if incoming.exists():
        shutil.rmtree(incoming)
    journal.unlink()
    _sync_dir(journal.parent)
    return True


def launch_installed(arguments=()):
    recover_update()
    environment = os.environ.copy()
    environment.pop('SWEETMETER_UPDATE_HEALTH', None)
    environment.pop('SWEETMETER_UPDATE_NONCE', None)
    process = subprocess.Popen(app_command() + list(arguments), env=environment,
                               cwd=str(install_root().parent))
    # launchd owns this stable launcher. Keep it alive while the child runs so
    # launchd does not reap its process group immediately after startup.
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
        plan = json.loads(self.plan_path.read_text())
        helper_name = 'sweetmeter-update-helper' + ('.exe' if sys.platform == 'win32' else '')
        source = resource_root() / helper_name
        if not source.is_file():
            raise RuntimeError('Updater helper missing; use the verified manual package')
        helper = self.plan_path.parent / helper_name
        shutil.copy2(source, helper)
        helper.chmod(0o700)
        kwargs = dict(cwd=str(self.plan_path.parent), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen([str(helper), str(self.plan_path)], **kwargs)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.plan_path.parent / 'helper-ready').exists():
                return
            time.sleep(.1)
        raise RuntimeError('Update helper did not start; current app has not been replaced')


def stage_update(package_path, artifact, state_dir):
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
    current_os = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
    arch = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    if artifact.get('os') != current_os or artifact.get('arch') != arch:
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
                    health_timeout=60, data_dir=str(data_dir()))
        plan_path = staging / 'plan.json'
        _write(plan_path, plan)
        return StagedUpdate(True, '', candidate, plan_path)
    except BaseException:
        shutil.rmtree(staging)
        raise


def confirm_update_health(version):
    """Call only after the new app has completed startup checks and started work."""
    marker = os.environ.get('SWEETMETER_UPDATE_HEALTH')
    nonce = os.environ.get('SWEETMETER_UPDATE_NONCE')
    if marker and nonce:
        path = Path(marker)
        _write(path, {'version': version, 'nonce': nonce, 'pid': os.getpid()})


def _alive(pid):
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
    # Copy before renaming so replacement stays on the same filesystem.
    incoming = root.with_name(root.name + '.incoming-' + plan['nonce'][:12])
    backup = root.with_name(root.name + '.previous-' + plan['nonce'][:12])
    if incoming.exists() or backup.exists():
        raise ValueError('Replacement paths already exist')
    health = plan_path.parent / 'healthy.json'
    outcome = Path(plan['state_dir']) / 'companion-update-result.json'
    journal = data_dir() / 'companion-swap.json'
    if journal.exists():
        raise RuntimeError('Another update recovery journal is already present')
    recovery = dict(schema=1, root=str(root), nonce=plan['nonce'],
                    state_dir=plan['state_dir'], version=plan['version'], stage='prepared')
    process = None
    replaced = False
    try:
        validate_tree(candidate)
        shutil.copytree(candidate, incoming, symlinks=True)
        for item in incoming.rglob('*'):
            if item.is_file() and not item.is_symlink():
                # Windows CRT _commit (os.fsync) requires a writable handle.
                # r+b preserves the bytes while allowing durable flush there.
                with item.open('r+b') as source:
                    os.fsync(source.fileno())
        _write(journal, recovery)
        root.rename(backup)
        _sync_dir(root.parent)
        recovery['stage'] = 'old_moved'
        _write(journal, recovery)
        incoming.rename(root)
        _sync_dir(root.parent)
        replaced = True
        recovery['stage'] = 'new_installed'
        _write(journal, recovery)
        environment = os.environ.copy()
        environment['SWEETMETER_UPDATE_HEALTH'] = str(health)
        environment['SWEETMETER_UPDATE_NONCE'] = plan['nonce']
        process = subprocess.Popen(app_command(root) + ['--state-dir', plan['state_dir']],
                                   env=environment, cwd=str(root.parent))
        deadline = time.monotonic() + min(120, max(10, int(plan['health_timeout'])))
        while time.monotonic() < deadline:
            try:
                value = json.loads(health.read_text())
            except (OSError, ValueError):
                value = {}
            if (value.get('version') == plan['version'] and value.get('nonce') == plan['nonce']
                    and value.get('pid') == process.pid and process.poll() is None):
                recovery['stage'] = 'confirmed'
                _write(journal, recovery)
                _write(outcome, {'status': 'success', 'version': plan['version']})
                shutil.rmtree(backup, ignore_errors=True)
                if not backup.exists():
                    journal.unlink()
                    _sync_dir(journal.parent)
                return
            if process.poll() is not None:
                raise RuntimeError('Updated app exited before confirming startup')
            time.sleep(.2)
        raise RuntimeError('Updated app did not confirm startup health')
    except BaseException as error:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if backup.exists():
            if replaced:
                shutil.rmtree(root)
            backup.rename(root)
            _sync_dir(root.parent)
            environment = os.environ.copy()
            environment.pop('SWEETMETER_UPDATE_HEALTH', None)
            environment.pop('SWEETMETER_UPDATE_NONCE', None)
            subprocess.Popen(app_command(root) + ['--state-dir', plan['state_dir']], env=environment, cwd=str(root.parent))
        _write(outcome, {'status': 'rollback', 'version': plan['version'], 'reason': str(error)})
        journal.unlink(missing_ok=True)
        _sync_dir(journal.parent)
        raise
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)
