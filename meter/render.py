"""A deliberately compact, single-page layout for the 250 x 122 panel."""
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

SIZE = (250, 122)
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


@lru_cache(maxsize=16)
def font(size, bold=False):
    """Use bundled OFL fonts for identical layout on every supported platform."""
    path = Path(__file__).resolve().parent / 'assets/fonts' / (
        'LiberationSans-Bold.ttf' if bold else 'LiberationSans-Regular.ttf')
    return ImageFont.truetype(str(path), size)


def compact(value):
    if value is None:
        return '--'
    for threshold, suffix in [(10**9, 'B'), (10**6, 'M'), (1000, 'K')]:
        if value >= threshold:
            return f'{value / threshold:.2f}'.rstrip('0').rstrip('.') + suffix
    return str(value)


def reset_countdown(reset, now):
    if reset is None:
        return '--d --h --m'
    remaining = reset - now
    if remaining <= 0:
        return 'reset pending'
    if remaining < 60:
        return '00d 00h <1m'
    days, remainder = divmod(int(remaining // 60), 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f'{days:02d}d {hours:02d}h {minutes:02d}m'


def reset_time(reset):
    if reset is None:
        return '--'
    value = datetime.fromtimestamp(reset)
    return f'{value.day:02d} {MONTHS[value.month - 1]} {value:%H:%M}'


def reset_group(draw, x, baseline, label, value, *, right=False, emphasis=False):
    label_font, value_font = font(9), font(10, emphasis)
    label_width = draw.textlength(label, font=label_font)
    width = label_width + 3 + draw.textlength(value, font=value_font)
    if right:
        x -= width
    draw.text((x, baseline), label, anchor='ls', font=label_font, fill=0)
    draw.text((x + label_width + 3, baseline), value, anchor='ls', font=value_font, fill=0)


def render(snapshot):
    image = Image.new('1', SIZE, 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 249, 12), fill=0)
    draw.text((3, 0), 'BT  USED%', font=font(9), fill=1)
    now = snapshot.get('clock_at', snapshot['as_of'])
    stamp = datetime.fromtimestamp(now).strftime('%Y/%m/%d %H:%M')
    draw.text((220, 0), stamp, anchor='ra', font=font(9), fill=1)
    battery = snapshot.get('device', {}).get('battery_percent', -1)
    draw.rectangle((229, 2, 245, 10), outline=1)
    draw.rectangle((246, 4, 247, 8), fill=1)
    if type(battery) is int and 0 <= battery <= 100:
        bars = (battery + 24) // 25
        for i in range(bars):
            draw.rectangle((231 + i * 3, 4, 232 + i * 3, 8), fill=1)
    else:
        draw.text((235, 0), '?', font=font(9), fill=1)
    for i, row in enumerate(snapshot['rows']):
        y = 15 + i * 27
        name, _, period = row['label'].rpartition(' ')
        if period not in ('5H', '7D'):
            name, period = row['label'], ''
        draw.text((4, y + 11), name, anchor='ls', font=font(11, True), fill=0)
        period_x = 4 + draw.textlength(name, font=font(11, True)) + 4
        draw.text((period_x, y + 11), period + ('!' if snapshot['stale'] else ''),
                  anchor='ls', font=font(9), fill=0)
        subscription = row.get('subscription_label')
        if subscription:
            draw.text((4, y + 23), subscription, anchor='ls', font=font(9), fill=0)
        used = row['used']
        value = '--' if used is None else f'{used:.1f}'.rstrip('0').rstrip('.') + '%'
        draw.text((247, y + 12), value, anchor='rm', font=font(15, True), fill=0)
        draw.rounded_rectangle((80, y, 201, y + 12), radius=3, outline=0)
        if used is not None:
            width = round(120 * max(0, min(100, used)) / 100)
            if width:
                draw.rounded_rectangle((81, y + 1, 80 + width, y + 11), radius=2, fill=0)
        # Invert the token glyphs against the actual fill for contrast at any %.
        token_mask = Image.new('1', SIZE, 0)
        ImageDraw.Draw(token_mask).text((141, y + 10), compact(row['tokens']) + ' TOKENS',
                                        anchor='ms', font=font(9), fill=1)
        image.paste(ImageChops.logical_xor(image, token_mask))
        countdown = reset_countdown(row['reset'], now)
        if countdown == 'reset pending':
            reset_group(draw, 80, y + 23, 'RESET:', 'PENDING')
        else:
            reset_group(draw, 80, y + 23, 'RESET IN:', countdown)
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
