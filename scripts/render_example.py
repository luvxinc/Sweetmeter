"""Render the approved illustrative dashboard without reading any account data."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from meter.render import render


def main():
    assets = ROOT / 'docs/assets'
    example = json.loads((assets / 'example-data.json').read_text())
    screen = render(example)
    screen.save(assets / 'dashboard-250x122.png')
    screen.resize((1000, 488), Image.Resampling.NEAREST).save(assets / 'dashboard.png')


if __name__ == '__main__':
    main()
