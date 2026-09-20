"""User-scoped paths; no developer machine identifiers or account credentials."""
import os
from pathlib import Path
import sys


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


def install_root():
    if sys.platform == 'darwin':
        return Path.home() / 'Applications/Sweetmeter.app'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Programs/Sweetmeter'
    return Path.home() / '.local/lib/Sweetmeter'


def app_command(install_path=None):
    root = Path(install_path) if install_path is not None else install_root()
    if sys.platform == 'darwin':
        return [str(root / 'Contents/MacOS/Sweetmeter')]
    return [str(root / ('Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter'))]


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
