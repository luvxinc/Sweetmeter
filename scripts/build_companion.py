"""Build native onedir apps; macOS framework links stay inside their app root."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile

from collect_notices import collect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from meter.self_update import validate_tree
from scripts.build_provenance import capture_build_state, require_unchanged_build_state, write_json_atomic


def run(*args):
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True)


def archive_tree(root, archive):
    validate_tree(root)
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                item = zipfile.ZipInfo(path.relative_to(root.parent).as_posix())
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(item, os.readlink(path))
            elif path.is_file() or path.is_dir():
                output.write(path, path.relative_to(root.parent).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--sign-identity', default=os.environ.get('MACOS_SIGN_IDENTITY'))
    args = parser.parse_args()
    provenance = capture_build_state(ROOT)
    os_name = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
    architecture = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    version = (ROOT / 'VERSION').read_text().strip()
    metadata = dict(schema=1, kind='sweetmeter-companion-build', version=version,
                    os=os_name, arch=architecture, test_build=False, **provenance)
    metadata['entrypoint'] = ('Sweetmeter.app/Contents/MacOS/Sweetmeter' if sys.platform == 'darwin'
                              else 'Sweetmeter/' + ('Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter'))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = ROOT / 'build/native'
    work.mkdir(parents=True, exist_ok=True)
    notices = work / 'NOTICES'
    if notices.exists():
        shutil.rmtree(notices)
    collect(notices)
    if not (ROOT / 'meter/assets/keys/release-1.pem').is_file():
        raise SystemExit('Trusted public release key is required; generate it before building.')
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
              '--workpath', str(work / 'work'), '--specpath', str(work),
              '--distpath', str(work / 'out'), '--paths', str(ROOT)]
    run(*common, '--onefile', '--name', 'sweetmeter-update-helper', ROOT / 'scripts/update_helper.py')
    helper_name = 'sweetmeter-update-helper' + ('.exe' if sys.platform == 'win32' else '')
    helper = work / 'out' / helper_name
    data = [(ROOT / 'meter/assets', 'meter/assets'), (ROOT / 'VERSION', '.'), (notices, 'NOTICES')]
    flags = []
    for source, destination in data:
        flags += ['--add-data', str(source) + os.pathsep + destination]
    flags += ['--add-binary', str(helper) + os.pathsep + '.', '--collect-all', 'bleak']
    if sys.platform == 'win32':
        flags.append('--noconsole')
    if sys.platform == 'darwin':
        flags += ['--windowed', '--osx-bundle-identifier', 'com.sweetmeter.companion',
                  '--codesign-identity', args.sign_identity or '-']
    run(*common, '--onedir', '--name', 'Sweetmeter', *flags, ROOT / 'packaging/companion_entry.py')
    raw = work / 'out' / ('Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter')
    normalized = work / 'normalized'
    if normalized.exists():
        shutil.rmtree(normalized)
    normalized.mkdir()
    if sys.platform == 'darwin':
        native = normalized / 'Sweetmeter.app'
        shutil.copytree(raw, native, symlinks=True)
        version = (ROOT / 'VERSION').read_text().strip()
        info_path = native / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info.update(dict(CFBundleName='Sweetmeter', CFBundleDisplayName='Sweetmeter',
                    CFBundleIdentifier='com.sweetmeter.companion', CFBundleExecutable='Sweetmeter',
                    CFBundlePackageType='APPL', CFBundleVersion=version,
                    CFBundleShortVersionString=version, NSHighResolutionCapable=True,
                    NSBluetoothAlwaysUsageDescription='Send quota data to your Sweetmeter and install updates you approve.',
                    NSBluetoothPeripheralUsageDescription='Connect to your Sweetmeter dashboard.'))
        info_path.write_bytes(plistlib.dumps(info))
        write_json_atomic(native / 'Contents/Resources/build-metadata.json', metadata)
        identity = args.sign_identity or '-'
        run('/usr/bin/xattr', '-cr', native)
        run('/usr/bin/codesign', '--force', '--deep', '--sign', identity,
            *(['--options', 'runtime', '--timestamp'] if args.sign_identity else []), native)
        run('/usr/bin/codesign', '--verify', '--deep', '--strict', native)
    else:
        native = normalized / 'Sweetmeter'
        shutil.copytree(raw, native, symlinks=False)
        write_json_atomic(native / '_internal/build-metadata.json', metadata)
    if architecture not in ('arm64', 'x86_64'):
        raise SystemExit('Unsupported release architecture: ' + architecture)
    require_unchanged_build_state(provenance, ROOT)
    archive = output / f'Sweetmeter-{version}-{os_name}-{architecture}.zip'
    archive_tree(native, archive)
    if sys.platform == 'darwin':
        executable = native / 'Contents/MacOS/Sweetmeter'
    else:
        executable = native / ('Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter')
    run(executable, '--version')
    run(executable, '--self-test')
    require_unchanged_build_state(provenance, ROOT)
    receipt = dict(metadata, artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                   executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
    write_json_atomic(archive.with_name(archive.name + '.build.json'), receipt)
    print(archive)


if __name__ == '__main__':
    main()
