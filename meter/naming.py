"""Meter names chosen by the owner (docs/PROTOCOL.md section 2.2).

The firmware validates exactly the bytes it receives; this module turns what
the owner typed into those bytes, or explains in plain words why it cannot.
"""
import re
import unicodedata

NAME_MAX_BYTES = 16
MENU_SUFFIX = '-PAIR'
DEFAULT_PATTERN = re.compile(r'Sweetmeter-[0-9A-F]{4}')
# Control characters the firmware refuses: C0, DEL and C1.
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


def encode_meter_name(text):
    """The UTF-8 bytes to send for ``text``; ValueError with a user-facing reason.

    An empty result means "restore the default name".
    """
    name = unicodedata.normalize('NFC', str(text or '')).strip()
    if not name:
        return b''
    if _CONTROL.search(name) or any(0xD800 <= ord(c) <= 0xDFFF for c in name):
        raise ValueError('The name contains characters that cannot be used.')
    if name.upper().endswith(MENU_SUFFIX):
        raise ValueError('The name cannot end with “-PAIR”.')
    data = name.encode('utf-8')
    if len(data) > NAME_MAX_BYTES:
        raise ValueError('The name is too long: use up to 16 letters or digits, or up to 5 Chinese characters.')
    return data


def display_name(local_name):
    """A meter's advertised name without the open-menu suffix, or ''."""
    if not isinstance(local_name, str):
        return ''
    return local_name[:-len(MENU_SUFFIX)] if local_name.endswith(MENU_SUFFIX) else local_name


def default_name(serial):
    """The name firmware uses without an owner-chosen one (from its serial)."""
    return 'Sweetmeter-' + serial[-4:].upper() if isinstance(serial, str) and len(serial) == 12 else ''
