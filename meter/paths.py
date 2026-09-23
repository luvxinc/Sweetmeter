"""User-scoped paths; no developer machine identifiers or account credentials."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

BUNDLE_ID = 'com.sweetmeter.companion'


def data_dir():
    if sys.platform == 'darwin':
        legacy = Path.home() / 'Library/Application Support/QuotaMeter'
        if (legacy / 'state/companion.json').is_file():
            return legacy  # Preserve the user's existing installation identity.
        return Path.home() / 'Library/Application Support/Sweetmeter'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Sweetmeter'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'sweetmeter'


def default_state_dir():
    return data_dir() / 'state'


def default_install_root():
    if sys.platform == 'darwin':
        return Path.home() / 'Applications/Sweetmeter.app'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Programs/Sweetmeter'
    return Path.home() / '.local/lib/Sweetmeter'


def candidate_install_roots():
    """Every location Sweetmeter may manage. Nothing else is ever adopted or removed.

    On macOS a user may drag the app into /Applications instead of
    ~/Applications; that copy can be adopted in place rather than duplicated.
    """
    roots = [default_install_root()]
    if sys.platform == 'darwin':
        roots.append(Path('/Applications/Sweetmeter.app'))
    return [root.absolute() for root in roots]


def read_install_record():
    """Bounded, fail-soft read of the per-user installation record."""
    path = data_dir() / 'install.json'
    try:
        if path.stat().st_size > 16384:
            return {}
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def install_root():
    """The managed native application root (recorded location, else the default)."""
    record = read_install_record()
    if record.get('kind') == 'native' and isinstance(record.get('root'), str):
        recorded = Path(record['root'])
        if recorded.is_absolute() and recorded.absolute() in candidate_install_roots():
            return recorded.absolute()
    return default_install_root()


def app_command(install_path=None):
    root = Path(install_path) if install_path is not None else install_root()
    if sys.platform == 'darwin':
        return [str(root / 'Contents/MacOS/Sweetmeter')]
    return [str(root / ('Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter'))]


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))


_ARCHITECTURES = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64', 'amd64': 'x86_64'}


def _macos_arm64_hardware():
    # A Rosetta-translated process reports x86_64; prefer the native package.
    try:
        return subprocess.run(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'], capture_output=True,
                              text=True, timeout=5).stdout.strip() == '1'
    except (OSError, subprocess.SubprocessError):
        return False


def platform_id():
    """(os, arch) of this computer using release-manifest names; arch None if unknown."""
    system = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
    machine = platform.machine()
    if sys.platform == 'win32':
        # A 64-bit Windows process under emulation may report its own
        # architecture; the native processor is the more useful answer.
        machine = os.environ.get('PROCESSOR_ARCHITEW6432') or machine
    arch = _ARCHITECTURES.get(machine.lower())
    if system == 'macos' and arch == 'x86_64' and _macos_arm64_hardware():
        arch = 'arm64'
    return system, arch


def compatible_platforms(system=None, arch=None):
    """Release packages this computer can run, most preferred first."""
    if system is None:
        system, arch = platform_id()
    if arch is None:
        return []
    choices = [(system, arch)]
    if system == 'windows' and arch == 'arm64':
        choices.append(('windows', 'x86_64'))  # Windows 11 on Arm emulates x64.
    return choices
