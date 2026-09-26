"""A deliberately compact, single-page layout for the 250 x 122 panel.

Every piece of variable text is fitted to a fixed box: it is shrunk to a
smaller size or shortened with an ellipsis, never drawn over its neighbours.
"""
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

SIZE = (250, 122)
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
ELLIPSIS = '…'
# Boxes (left, right) in pixels for the fixed layout.
FIRMWARE_BOX = (3, 86)       # Header text before the "BT" label at x=90.
NAME_BOX = (4, 77)           # Row name + period, left of the bar at x=80.
BAR_BOX = (80, 201)
PERCENT_BOX = (205, 247)     # Right of the bar.
RESET_BOX = (80, 247)


@lru_cache(maxsize=16)
def font(size, bold=False):
    """Use bundled OFL fonts for identical layout on every supported platform."""
    path = Path(__file__).resolve().parent / 'assets/fonts' / (
        'LiberationSans-Bold.ttf' if bold else 'LiberationSans-Regular.ttf')
    return ImageFont.truetype(str(path), size)


def printable(text):
    """Keep characters the bundled Latin font can draw; drop controls and
    anything (CJK, emoji) that would render as an empty box."""
    if text is None:
        return ''
    text = str(text)
    out = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7e or 0xa0 <= code <= 0x17f or ch in '×…–—‘’“”•':
            out.append(ch)
        elif ch.isspace():
            out.append(' ')
    return ' '.join(''.join(out).split())


def fit(draw, text, width, sizes, bold=False):
    """Return (text, font) that fits ``width``: shrink first, then shorten."""
    for size in sizes:
        face = font(size, bold)
        if draw.textlength(text, font=face) <= width:
            return text, face
    face = font(sizes[-1], bold)
    while text and draw.textlength(text + ELLIPSIS, font=face) > width:
        text = text[:-1]
    return (text.rstrip() + ELLIPSIS) if text else '', face


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def compact(value):
    """Short token count: 999 -> '999', 999_999 -> '1M', 1_280_000 -> '1.28M'."""
    if not _number(value):
        return '--'
    value = max(0, value)
    if value < 999.5:
        return str(int(round(value)))
    units = [(1000, 'K'), (10**6, 'M'), (10**9, 'B'), (10**12, 'T')]
    for index, (threshold, suffix) in enumerate(units):
        last = index == len(units) - 1
        if not last and value >= units[index + 1][0]:
            continue
        text = f'{value / threshold:.2f}'.rstrip('0').rstrip('.')
        if float(text) >= 1000 and not last:
            continue  # Rounding reached the next unit: 999.999K is 1M.
        return text + suffix
    return '--'


def percent(used):
    """'38%', '0.5%', '150%'; at most four glyphs before the sign."""
    if not _number(used):
        return '--'
    used = max(0.0, float(used))
    if used >= 999.5:
        return '999+%'
    if used >= 99.95:
        return f'{used:.0f}%'
    return f'{used:.1f}'.rstrip('0').rstrip('.') + '%'


def reset_countdown(reset, now):
    if not _number(reset):
        return '--d --h --m'
    remaining = reset - now
    if remaining <= 0:
        return 'reset pending'
    if remaining < 60:
        return '00d 00h <1m'
    days, remainder = divmod(int(remaining // 60), 24 * 60)
    if days > 99:
        return '>99 days'
    hours, minutes = divmod(remainder, 60)
    return f'{days:02d}d {hours:02d}h {minutes:02d}m'


def reset_time(reset):
    if not _number(reset):
        return '--'
    value = datetime.fromtimestamp(reset)
    return f'{value.day:02d} {MONTHS[value.month - 1]} {value:%H:%M}'


def reset_group(draw, x, baseline, label, value, *, right=False, emphasis=False, width=None):
    label_font = font(9)
    label_width = draw.textlength(label, font=label_font)
    available = (width if width is not None else RESET_BOX[1] - x) - label_width - 3
    value, value_font = fit(draw, value, available, (10, 9), emphasis)
    total = label_width + 3 + draw.textlength(value, font=value_font)
    if right:
        x -= total
    draw.text((x, baseline), label, anchor='ls', font=label_font, fill=0)
    draw.text((x + label_width + 3, baseline), value, anchor='ls', font=value_font, fill=0)


def row_state(row, snapshot):
    """'ok', 'stale' or 'absent'. Rows without a state use the legacy flag."""
    state = row.get('state')
    if state in ('ok', 'stale', 'absent'):
        return state
    return 'stale' if snapshot.get('stale') else 'ok'


def render(snapshot):
    image = Image.new('1', SIZE, 1)
    draw = ImageDraw.Draw(image)
    device = snapshot.get('device') or {}
    if not isinstance(device, dict):
        device = {}
    draw.rectangle((0, 0, 249, 12), fill=0)
    firmware = printable(device.get('firmware')) or '--'
    text, face = fit(draw, 'v ' + firmware, FIRMWARE_BOX[1] - FIRMWARE_BOX[0], (9, 8))
    draw.text((FIRMWARE_BOX[0], 0), text, font=face, fill=1)
    draw.text((90, 0), 'BT', font=font(9), fill=1)
    rssi = device.get('rssi', 127)
    bars = (4 if rssi >= -60 else 3 if rssi >= -70 else 2 if rssi >= -80 else 1) if type(rssi) is int and -127 <= rssi <= 20 else 0
    for i in range(4):
        height = 2 + i * 2 if i < bars else 1
        draw.rectangle((105 + i * 4, 10 - height, 106 + i * 4, 9), fill=1)
    now = snapshot.get('clock_at', snapshot['as_of'])
    stamp = datetime.fromtimestamp(now).strftime('%Y/%m/%d %H:%M')
    draw.text((220, 0), stamp, anchor='ra', font=font(9), fill=1)
    battery = device.get('battery_percent', -1)
    draw.rectangle((229, 2, 245, 10), outline=1)
    draw.rectangle((246, 4, 247, 8), fill=1)
    if type(battery) is int and 0 <= battery <= 100:
        bars = (battery + 24) // 25
        for i in range(bars):
            draw.rectangle((231 + i * 3, 4, 232 + i * 3, 8), fill=1)
    else:
        draw.text((235, 0), '?', font=font(9), fill=1)
    for i, row in enumerate(snapshot['rows'][:4]):
        y = 15 + i * 27
        state = row_state(row, snapshot)
        label = printable(row.get('label')) or '--'
        name, _, period = label.rpartition(' ')
        if period not in ('5H', '7D'):
            name, period = label, ''
        suffix = period + ('!' if state == 'stale' else '')
        suffix_width = draw.textlength(suffix, font=font(9)) + 4 if suffix else 0
        name, name_font = fit(draw, name, NAME_BOX[1] - NAME_BOX[0] - suffix_width, (11, 10), True)
        draw.text((NAME_BOX[0], y + 11), name, anchor='ls', font=name_font, fill=0)
        if suffix:
            period_x = NAME_BOX[0] + draw.textlength(name, font=name_font) + 4
            draw.text((period_x, y + 11), suffix, anchor='ls', font=font(9), fill=0)
        if state == 'absent':
            text, face = fit(draw, 'NOT SET UP', NAME_BOX[1] - NAME_BOX[0], (9,))
            draw.text((NAME_BOX[0], y + 23), text, anchor='ls', font=face, fill=0)
            draw.rounded_rectangle((BAR_BOX[0], y, BAR_BOX[1], y + 12), radius=3, outline=0)
            draw.text(((BAR_BOX[0] + BAR_BOX[1]) // 2 + 1, y + 10), 'NO DATA', anchor='ms', font=font(9), fill=0)
            draw.text((PERCENT_BOX[1], y + 12), '--', anchor='rm', font=font(15, True), fill=0)
            if i < 3:
                draw.line((4, y + 25, 245, y + 25), fill=0)
            continue
        subscription = printable(row.get('subscription_label'))
        if subscription:
            text, face = fit(draw, subscription, NAME_BOX[1] - NAME_BOX[0], (9, 8))
            draw.text((NAME_BOX[0], y + 23), text, anchor='ls', font=face, fill=0)
        used = row.get('used')
        value, value_font = fit(draw, percent(used), PERCENT_BOX[1] - PERCENT_BOX[0], (15, 13, 11), True)
        draw.text((PERCENT_BOX[1], y + 12), value, anchor='rm', font=value_font, fill=0)
        draw.rounded_rectangle((BAR_BOX[0], y, BAR_BOX[1], y + 12), radius=3, outline=0)
        if _number(used):
            width = round(120 * max(0, min(100, used)) / 100)
            if width:
                draw.rounded_rectangle((81, y + 1, 80 + width, y + 11), radius=2, fill=0)
        # Invert the token glyphs against the actual fill for contrast at any %.
        token_mask = Image.new('1', SIZE, 0)
        mask = ImageDraw.Draw(token_mask)
        text, face = fit(mask, compact(row.get('tokens')) + ' TOKENS', BAR_BOX[1] - BAR_BOX[0] - 6, (9, 8))
        mask.text((141, y + 10), text, anchor='ms', font=face, fill=1)
        image.paste(ImageChops.logical_xor(image, token_mask))
        countdown = reset_countdown(row.get('reset'), now)
        if countdown == 'reset pending':
            reset_group(draw, BAR_BOX[0], y + 23, 'RESET:', 'PENDING')
        else:
            reset_group(draw, BAR_BOX[0], y + 23, 'RESET IN:', countdown)
        if i < 3:
            draw.line((4, y + 25, 245, y + 25), fill=0)
    return image


def pack_frame(image):
    """Map the upright layout to the V1.2 panel's mirrored landscape scan."""
    if image.size != SIZE:
        raise ValueError('Expected 250 x 122 image')
    image = image.convert('1')
    frame = bytearray(b'\xff' * 4000)
    for y in range(122):
        for x in range(250):
            if not image.getpixel((x, y)):
                frame[x * 16 + y // 8] &= ~(0x80 >> (y % 8))
    return bytes(frame)
