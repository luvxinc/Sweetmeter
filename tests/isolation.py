"""Process-wide sandbox imported first by every test module.

`python -m unittest discover -s tests` imports each test module before any
test runs; each one starts with `import isolation`, so this runs before the
first test (test_isolation.py fails if a module does not import it).

- HOME, USERPROFILE, LOCALAPPDATA, APPDATA and XDG_* point into one private
  temporary folder for the whole run; account/profile overrides are removed.
  Nothing can read or write the real user's Sweetmeter data, launch agents,
  Claude/Codex logins or logs through an unpatched path lookup.
- Starting launchctl, open, security, reg, osascript, systemctl or codex
  raises IsolationViolation (a BaseException, so no `except Exception` in
  product code can hide it) unless the test patched the launch boundary
  itself. Every way to start a program is guarded: `subprocess`, `os.system`,
  `os.posix_spawn[p]`, `os.exec*` and `os.spawn*`. The whole argv is
  inspected, so wrappers such as `env security …`, `sh -c "launchctl …"` or
  `nohup open …` are refused as well.
- On Windows, writes to the real registry (winreg) are refused.
- Network connections to anything but this computer (loopback or a local
  socket file) are refused, as are name lookups of other hosts, unless a test
  opts in with `with isolation.allow_network(): …`.
"""
import atexit
from contextlib import contextmanager
import ipaddress
import os
from pathlib import PurePath
import re
import shutil
import socket
import subprocess
import tempfile

BLOCKED = frozenset({'launchctl', 'open', 'security', 'reg', 'osascript', 'systemctl', 'codex'})
# Programs that run another program named in their arguments or a script.
WRAPPERS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'csh', 'tcsh', 'env', 'cmd', 'powershell',
                      'pwsh', 'nohup', 'xargs', 'sudo', 'doas', 'timeout', 'nice', 'exec', 'command',
                      'arch', 'caffeinate', 'script', 'start', 'su', 'setsid', 'stdbuf', 'time'})
ACTIVE = True


class IsolationViolation(BaseException):
    """A test reached a real system service, account store or the network."""


def _name(token):
    name = PurePath(os.fsdecode(token).strip('"\'').replace('\\', '/')).name.lower()
    for suffix in ('.exe', '.cmd', '.bat', '.com', '.ps1'):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


def _words(text):
    """Every word of shell text, split at whitespace and shell syntax."""
    return [word for word in re.split(r'[\s;&|()<>`$]+', os.fsdecode(text)) if word]


def blocked_program(args):
    """The blocked program an argv (or shell command string) would run, else None.

    The first word is always checked. When it is a shell or wrapper, every
    word of every argument is checked too, because `sh -c "…"` or `env A=1 …`
    runs a program that is not argv[0]."""
    if isinstance(args, (str, bytes, os.PathLike)):
        words = _words(args)
        shell_text = True
    else:
        words = [os.fsdecode(arg) for arg in args]
        shell_text = False
    if not words:
        return None
    first = _name(words[0])
    if first in BLOCKED:
        return first
    if shell_text or first in WRAPPERS:
        for argument in words[1:]:
            for word in _words(argument):
                word = word.split('=', 1)[1] if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', word) else word
                if _name(word) in BLOCKED:
                    return _name(word)
    return None


def _check(args):
    program = blocked_program(args)
    if ACTIVE and program:
        raise IsolationViolation('Test tried to run the real %r; patch the launch in this test' % program)


_RealPopen = subprocess.Popen


class GuardedPopen(_RealPopen):
    def __init__(self, args, *positional, **keywords):
        _check(args)
        super().__init__(args, *positional, **keywords)


if getattr(subprocess.Popen, '__name__', '') != 'GuardedPopen':
    subprocess.Popen = GuardedPopen


def _guard_os():
    def guarded(name, pick):
        real = getattr(os, name, None)
        if real is None or getattr(real, '_sweetmeter_guarded', False):
            return

        def wrapper(*args, **kwargs):
            _check(pick(args))
            return real(*args, **kwargs)
        wrapper.__name__, wrapper.__doc__ = name, real.__doc__
        wrapper._sweetmeter_guarded = True
        setattr(os, name, wrapper)

    guarded('system', lambda a: a[0])
    guarded('popen', lambda a: a[0])
    for name in ('posix_spawn', 'posix_spawnp', 'execv', 'execve', 'execvp', 'execvpe'):
        guarded(name, lambda a: [a[0], *list(a[1])[1:]])
    for name in ('execl', 'execle', 'execlp', 'execlpe'):
        guarded(name, lambda a: [a[0], *a[2:]])
    for name in ('spawnv', 'spawnve', 'spawnvp', 'spawnvpe'):
        guarded(name, lambda a: [a[1], *list(a[2])[1:]])
    for name in ('spawnl', 'spawnle', 'spawnlp', 'spawnlpe'):
        guarded(name, lambda a: [a[1], *a[3:]])


_guard_os()


def _guard_registry():
    """Windows only: the real registry is never written by a test. Tests
    that exercise registry code install a fake `winreg` module instead."""
    try:
        import winreg
    except ImportError:
        return
    for name in ('SetValue', 'SetValueEx', 'CreateKey', 'CreateKeyEx', 'DeleteKey', 'DeleteKeyEx',
                 'DeleteValue'):
        real = getattr(winreg, name, None)
        if real is None or getattr(real, '_sweetmeter_guarded', False):
            continue

        def refuse(*args, _name=name, **kwargs):
            if ACTIVE:
                raise IsolationViolation('Test tried to write the real Windows registry (%s); '
                                         'use a fake winreg module' % _name)
            return getattr(winreg, '_real_' + _name)(*args, **kwargs)
        refuse._sweetmeter_guarded = True
        setattr(winreg, '_real_' + name, real)
        setattr(winreg, name, refuse)


_guard_registry()

# --- Network -----------------------------------------------------------------
_network_everywhere = [0]


@contextmanager
def allow_network():
    """Explicit opt-in for a test that must reach a non-local host."""
    _network_everywhere[0] += 1
    try:
        yield
    finally:
        _network_everywhere[0] -= 1


def network_allowed():
    return not ACTIVE or _network_everywhere[0] > 0


def _local_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    host = str(host).strip('[]').split('%', 1)[0].lower()
    if host in ('', 'localhost', 'localhost.', 'ip6-localhost'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(address, 'ipv4_mapped', None):
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _local_address(address):
    if isinstance(address, (str, bytes, os.PathLike)):
        return True  # AF_UNIX socket file on this computer.
    if isinstance(address, tuple) and address:
        return _local_host(address[0])
    return False


def _refuse_network(target):
    raise IsolationViolation('Test tried to reach the network (%r); use a fake, loopback, '
                             'or `with isolation.allow_network():`' % (target,))


_RealSocket = socket.socket
if not getattr(_RealSocket.connect, '_sweetmeter_guarded', False):
    _real_connect, _real_connect_ex = _RealSocket.connect, _RealSocket.connect_ex
    _real_sendto = _RealSocket.sendto

    def _connect(self, address):
        if not network_allowed() and not _local_address(address):
            _refuse_network(address)
        return _real_connect(self, address)

    def _connect_ex(self, address):
        if not network_allowed() and not _local_address(address):
            _refuse_network(address)
        return _real_connect_ex(self, address)

    def _sendto(self, data, *rest):
        address = rest[-1] if rest else None
        if not network_allowed() and not _local_address(address):
            _refuse_network(address)
        return _real_sendto(self, data, *rest)

    for _function in (_connect, _connect_ex, _sendto):
        _function._sweetmeter_guarded = True
    _RealSocket.connect, _RealSocket.connect_ex, _RealSocket.sendto = _connect, _connect_ex, _sendto

if not getattr(socket.getaddrinfo, '_sweetmeter_guarded', False):
    _real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, *args, **kwargs):
        if not network_allowed() and not _local_host(host):
            _refuse_network(host)
        return _real_getaddrinfo(host, *args, **kwargs)
    _getaddrinfo._sweetmeter_guarded = True
    socket.getaddrinfo = _getaddrinfo

# platform.uname() is computed once and cached. Warm it with the real
# environment so a test that clears os.environ cannot poison the cache.
import platform as _platform
_platform.uname()

# --- Private home ------------------------------------------------------------
SANDBOX = tempfile.mkdtemp(prefix='sweetmeter-tests-')
atexit.register(shutil.rmtree, SANDBOX, True)
_FORCED = {
    'HOME': 'home', 'USERPROFILE': 'home', 'LOCALAPPDATA': 'home/AppData/Local',
    'APPDATA': 'home/AppData/Roaming', 'XDG_DATA_HOME': 'home/.local/share',
    'XDG_CONFIG_HOME': 'home/.config', 'XDG_STATE_HOME': 'home/.local/state',
    'XDG_CACHE_HOME': 'home/.cache',
}
for _key, _relative in _FORCED.items():
    _path = os.path.join(SANDBOX, *_relative.split('/'))
    os.makedirs(_path, exist_ok=True)
    os.environ[_key] = _path
for _key in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN', 'CODEX_HOME',
             'SWEETMETER_CODEX_PATH', 'SWEETMETER_UPDATE_HEALTH', 'SWEETMETER_UPDATE_NONCE',
             'SWEETMETER_UPDATE_BLUETOOTH', 'SWEETMETER_UPDATE_DEADLINE'):
    os.environ.pop(_key, None)
