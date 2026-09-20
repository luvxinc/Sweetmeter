"""Archive and restore the exact CI firmware bytes used for hardware acceptance."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.sign_release import verify_firmware_build

BUILD = PurePosixPath('firmware/.pio/build/crowpanel213')
GENERATED = PurePosixPath('firmware/.generated')
REQUIRED = {str(BUILD / name) for name in ('firmware.bin', 'firmware.bin.build.json',
                                         'firmware.elf', 'bootloader.bin', 'partitions.bin', 'boot_app0.bin')}
REQUIRED.add(str(GENERATED / 'sweetmeter_release.h'))
MAX_SIZE = 512 * 1024 * 1024


def allowed(name):
    if '\\' in name or ':' in name or '\0' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        return False
    under_build = BUILD in path.parents and path.suffix in {'.bin', '.elf', '.map', '.o', '.a', '.json'}
    under_generated = GENERATED in path.parents and path.suffix in {'.h', '.json'}
    return under_build or under_generated


def check_entries(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if (not entries or len(entries) > 20000 or len(set(n.casefold() for n in names)) != len(names)
            or not REQUIRED.issubset(names) or not any(n.endswith('.o') for n in names)
            or sum(e.file_size for e in entries) > MAX_SIZE):
        raise ValueError('Incomplete or oversized firmware build archive')
    for entry in entries:
        if (not allowed(entry.filename) or entry.is_dir()
                or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
            raise ValueError('Unsafe firmware build archive entry')
    return entries


def create(output, root=ROOT):
    output, root = Path(output), Path(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    boot_app = root / BUILD / 'boot_app0.bin'
    if not boot_app.exists():
        core = Path(os.environ.get('PLATFORMIO_CORE_DIR', Path.home() / '.platformio'))
        shutil.copy2(core / 'packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin', boot_app)
    paths = []
    for relative in (BUILD, GENERATED):
        for path in (root / relative).rglob('*'):
            if path.is_symlink():
                raise ValueError('Firmware build archive cannot contain symlinks')
            if path.is_file() and allowed(path.relative_to(root).as_posix()):
                paths.append(path)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(output) as archive:
        check_entries(archive)
    print(output)


def restore(archive_path, image_sha256, commit, root=ROOT):
    root = Path(root).resolve()
    if not re.fullmatch('[0-9a-f]{64}', image_sha256) or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Use the accepted image SHA256 and exact source commit SHA')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if head != commit:
        raise ValueError('Firmware restore checkout does not match accepted commit')
    with zipfile.ZipFile(archive_path) as archive:
        entries = check_entries(archive)
        image = archive.read(str(BUILD / 'firmware.bin'))
        if hashlib.sha256(image).hexdigest() != image_sha256:
            raise ValueError('CI firmware does not match the hardware-accepted image digest')
        # Validate all paths before writing; output is restricted to ignored build
        # folders, never source, credentials, startup state or signing keys.
        for entry in entries:
            target = root / entry.filename
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise ValueError('Refusing a symlinked firmware restore path')
        for entry in entries:
            target = root / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip()
    version = (root / 'VERSION').read_text().strip()
    verify_firmware_build(root / BUILD / 'firmware.bin', version=version, root_version=version,
                          source_commit=commit, source_tree=tree)
    print('Restored accepted firmware SHA256 ' + image_sha256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('create')
    pack.add_argument('--output', type=Path, required=True)
    unpack = commands.add_parser('restore')
    unpack.add_argument('--archive', type=Path, required=True)
    unpack.add_argument('--sha256', required=True)
    unpack.add_argument('--commit', required=True)
    args = parser.parse_args()
    if args.command == 'create':
        create(args.output)
    else:
        restore(args.archive, args.sha256, args.commit)


if __name__ == '__main__':
    main()
