"""Collect the actual build environment's redistributable dependency notices."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def collect(destination):
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'LICENSE', destination / 'Sweetmeter-Apache-2.0.txt')
    shutil.copy2(ROOT / 'THIRD_PARTY_NOTICES.md', destination / 'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT / 'meter/assets/fonts/LICENSE', destination / 'Fonts-OFL-1.1.txt')
    records = []
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = distribution.metadata['Name']
        folder = destination / name
        copied = []
        for entry in distribution.files or []:
            relative = Path(str(entry))
            if '..' in relative.parts:
                continue
            base = relative.name.lower()
            if (base.startswith(('license', 'licence', 'copying', 'copyright', 'notice'))
                    or 'licenses' in [p.lower() for p in relative.parts]):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    if source.suffix.lower() in {'.so', '.dll', '.dylib', '.py', '.pyc', '.pyd', '.exe'}:
                        continue
                    raw = source.read_bytes()
                    if b'\x00' in raw:
                        continue  # e.g. PyObjC's copying test binary is not a license.
                    try:
                        raw.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    target = folder / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(str(relative))
        records.append(dict(name=name, version=distribution.version,
                            license=distribution.metadata.get('License-Expression') or distribution.metadata.get('License'),
                            notices=copied))
    # Wheels often ship native-library notices under their own *.dist-info/licenses.
    # Record exact versions, including the build tools, so the build can be audited.
    (destination / 'dependencies.json').write_text(json.dumps(records, indent=2) + '\n')
    required = {'pillow', 'requests', 'bleak', 'cryptography', 'pyinstaller'}
    present = {r['name'].lower(): r for r in records}
    for name in required:
        if name not in present or not present[name]['notices']:
            raise RuntimeError('Dependency license files missing: ' + name)
    for source in (ROOT / 'packaging/licenses').glob('*'):
        if source.is_file():
            shutil.copy2(source, destination / source.name)
    python_license = next((p for p in (Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt',
                                      Path(sys.base_prefix) / 'LICENSE.txt') if p.is_file()), None)
    if python_license is None:
        raise RuntimeError('CPython runtime license file is missing')
    shutil.copy2(python_license, destination / 'CPython-LICENSE.txt')
    (destination / 'python-version.txt').write_text(sys.version + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    collect(parser.parse_args().output)
