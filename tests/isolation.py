"""Process-wide sandbox imported first by every test module.

`python -m unittest discover -s tests` imports each test module before any
test runs; each one starts with `import isolation`, so this runs before the
first test (test_isolation.py fails if a module does not import it).

- HOME, USERPROFILE, LOCALAPPDATA, APPDATA and XDG_* point into one private
  temporary folder for the whole run; account/profile overrides are removed.
  Nothing can read or write the real user's Sweetmeter data, launch agents,
  Claude/Codex logins or logs through an unpatched path lookup.
- Starting launchctl, open, security, reg, osascript, systemctl or codex
  through `subprocess` raises IsolationViolation (a BaseException, so no
  `except Exception` in product code can hide it) unless the test patched
  `subprocess.Popen`/`subprocess.run` itself.
"""
import atexit
import os
from pathlib import PurePath
import shutil
import subprocess
import tempfile

BLOCKED = frozenset({'launchctl', 'open', 'security', 'reg', 'osascript', 'systemctl', 'codex'})
ACTIVE = True


class IsolationViolation(BaseException):
    """A test reached a real system service or account store."""


def _program(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        first = os.fsdecode(args).split()[0] if os.fsdecode(args).split() else ''
    else:
        args = list(args)
        first = os.fsdecode(args[0]) if args else ''
    name = PurePath(first.replace('\\', '/')).name.lower()
    for suffix in ('.exe', '.cmd', '.bat'):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


_RealPopen = subprocess.Popen


class GuardedPopen(_RealPopen):
    def __init__(self, args, *positional, **keywords):
        program = _program(args)
        if program in BLOCKED:
            raise IsolationViolation('Test tried to run the real %r; patch subprocess in this test' % program)
        super().__init__(args, *positional, **keywords)


if getattr(subprocess.Popen, '__name__', '') != 'GuardedPopen':
    subprocess.Popen = GuardedPopen

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
