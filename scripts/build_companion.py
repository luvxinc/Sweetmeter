"""Build native onedir apps; macOS framework links stay inside their app root."""
import argparse
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from collect_notices import collect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from meter.self_update import validate_tree
from scripts.build_provenance import capture_build_state, require_unchanged_build_state, write_json_atomic


MACOS_MINIMUM = '15.0'
ENTITLEMENTS = ROOT / 'packaging/macos-entitlements.plist'


def run(*args):
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True)


def signing_kind(identity):
    """'adhoc', 'developer-id' (notarizable) or 'stable' (e.g. a self-signed certificate)."""
    if not identity or identity == '-':
        return 'adhoc'
    return 'developer-id' if identity.startswith('Developer ID Application:') else 'stable'


def codesign_options(identity):
    if signing_kind(identity) != 'developer-id':
        return []
    # Hardened runtime and a secure timestamp are required for notarization.
    return ['--options', 'runtime', '--timestamp', '--entitlements', str(ENTITLEMENTS)]


def designated_requirement(app):
    result = subprocess.run(['/usr/bin/codesign', '-d', '-r-', str(app)], capture_output=True, text=True)
    text = result.stdout + result.stderr
    lines = [line for line in text.splitlines() if 'designated =>' in line]
    return lines[0].split('designated =>', 1)[1].strip() if lines else ''


def check_designated_requirement(app, identity):
    """TCC keys permissions to the designated requirement. An ad-hoc one names
    the cdhash, which changes every build, so macOS forgets Bluetooth
    permission at each update. A certificate-based one stays stable."""
    requirement = designated_requirement(app)
    print('macOS designated requirement:', requirement or '(none)')
    if signing_kind(identity) != 'adhoc' and (not requirement or 'cdhash' in requirement):
        raise SystemExit('Signing identity did not produce a certificate-based designated requirement')
    if signing_kind(identity) == 'adhoc':
        print('WARNING: ad-hoc signature; macOS asks for Bluetooth permission again after each update. '
              'Configure a stable signing identity (docs/RELEASING.md).')
    return requirement


def _secret_file(name, data):
    folder = Path(os.environ.get('RUNNER_TEMP') or tempfile.gettempdir())
    path = folder / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
    return path


def notarize(app, identity):
    """Notarize and staple when App Store Connect API credentials are configured."""
    values = [os.environ.get(key, '') for key in ('APPLE_NOTARY_KEY', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER')]
    if not any(values):
        print('Notarization credentials not configured: the app is not notarized.')
        return False
    if not all(values):
        raise SystemExit('APPLE_NOTARY_KEY, APPLE_NOTARY_KEY_ID and APPLE_NOTARY_ISSUER must be configured together')
    if signing_kind(identity) != 'developer-id':
        raise SystemExit('Notarization requires a Developer ID Application signing identity')
    key_text, key_id, issuer = values
    try:
        key = base64.b64decode(key_text, validate=True)
    except ValueError:
        key = key_text.encode('utf-8')  # Accept the raw .p8 PEM text as well.
    key_path = _secret_file('sweetmeter-notary-key.p8', key)
    submission = Path(tempfile.mkdtemp(prefix='sweetmeter-notary-')) / 'Sweetmeter.zip'
    try:
        run('/usr/bin/ditto', '-c', '-k', '--keepParent', app, submission)
        result = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(submission), '--key', str(key_path),
                                 '--key-id', key_id, '--issuer', issuer, '--wait', '--timeout', '45m',
                                 '--output-format', 'json'], capture_output=True, text=True)
        try:
            outcome = json.loads(result.stdout or '{}')
        except ValueError:
            outcome = {}
        if result.returncode or outcome.get('status') != 'Accepted':
            print('Notarization status:', outcome.get('status', 'unknown'), 'id:', outcome.get('id', '-'))
            if outcome.get('id'):
                subprocess.run(['/usr/bin/xcrun', 'notarytool', 'log', outcome['id'], '--key', str(key_path),
                                '--key-id', key_id, '--issuer', issuer])
            raise SystemExit('Apple notarization was not accepted')
        run('/usr/bin/xcrun', 'stapler', 'staple', app)
        run('/usr/bin/xcrun', 'stapler', 'validate', app)
        run('/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=2', app)
    finally:
        key_path.unlink(missing_ok=True)
        shutil.rmtree(submission.parent, ignore_errors=True)
    return True


def signtool():
    found = shutil.which('signtool')
    if found:
        return found
    kits = sorted(glob.glob(r'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe'))
    if not kits:
        raise SystemExit('signtool.exe (Windows SDK) is required for Authenticode signing')
    return kits[-1]


def authenticode(*paths):
    """Sign Windows executables when an Authenticode certificate is configured."""
    certificate = os.environ.get('WINDOWS_CERTIFICATE_PFX', '')
    password = os.environ.get('WINDOWS_CERTIFICATE_PASSWORD', '')
    if not certificate and not password:
        return False
    if not (certificate and password):
        raise SystemExit('WINDOWS_CERTIFICATE_PFX and WINDOWS_CERTIFICATE_PASSWORD must be configured together')
    timestamp = os.environ.get('WINDOWS_TIMESTAMP_URL') or 'http://timestamp.digicert.com'
    pfx = _secret_file('sweetmeter-authenticode.pfx', base64.b64decode(certificate, validate=True))
    try:
        tool = signtool()
        for path in paths:
            result = subprocess.run([tool, 'sign', '/fd', 'sha256', '/f', str(pfx), '/p', password,
                                     '/tr', timestamp, '/td', 'sha256', str(path)], capture_output=True, text=True)
            if result.returncode:
                # Never echo the command line: it contains the certificate password.
                raise SystemExit('Authenticode signing failed for ' + Path(path).name + ': ' + result.stdout[-400:])
            run(tool, 'verify', '/pa', path)
    finally:
        pfx.unlink(missing_ok=True)
    return True


def check_crypto_linkage():
    if sys.platform == 'darwin' and platform.machine().lower() == 'x86_64':
        from cryptography.hazmat.bindings import _rust
        dependencies = subprocess.check_output(['otool', '-L', _rust.__file__], text=True)
        if any('/libssl' in line or '/libcrypto' in line for line in dependencies.splitlines()[1:]):
            raise RuntimeError('Intel cryptography must link OpenSSL statically; use scripts/setup_native_env.py')


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
    check_crypto_linkage()
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
    try:
        from meter.protocol import trusted_keys
        trusted_keys()  # Every shipped key must parse; the current signing key must exist.
    except (OSError, ValueError) as error:
        raise SystemExit('Trusted public release keys are invalid or missing: ' + str(error))
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
              '--workpath', str(work / 'work'), '--specpath', str(work),
              '--distpath', str(work / 'out'), '--paths', str(ROOT)]
    macos_signing = []
    if sys.platform == 'darwin':
        macos_signing = ['--codesign-identity', args.sign_identity or '-']
        if signing_kind(args.sign_identity) == 'developer-id':
            macos_signing += ['--osx-entitlements-file', str(ENTITLEMENTS)]
    helper_flags = ['--add-data', str(ROOT / 'VERSION') + os.pathsep + '.']
    if sys.platform == 'win32':
        # The launcher/update helper runs in the background: never open a console.
        helper_flags.append('--noconsole')
    run(*common, '--onefile', '--name', 'sweetmeter-update-helper', *helper_flags, *macos_signing,
        ROOT / 'scripts/update_helper.py')
    helper_name = 'sweetmeter-update-helper' + ('.exe' if sys.platform == 'win32' else '')
    helper = work / 'out' / helper_name
    if sys.platform == 'win32':
        authenticode(helper)
    data = [(ROOT / 'meter/assets', 'meter/assets'), (ROOT / 'VERSION', '.'), (notices, 'NOTICES')]
    flags = []
    for source, destination in data:
        flags += ['--add-data', str(source) + os.pathsep + destination]
    flags += ['--add-binary', str(helper) + os.pathsep + '.', '--collect-all', 'bleak']
    if sys.platform == 'win32':
        flags.append('--noconsole')
    if sys.platform == 'darwin':
        flags += ['--windowed', '--osx-bundle-identifier', 'com.sweetmeter.companion', *macos_signing]
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
                    LSMinimumSystemVersion=MACOS_MINIMUM,
                    NSBluetoothAlwaysUsageDescription='Send quota data to your Sweetmeter and install updates you approve.',
                    NSBluetoothPeripheralUsageDescription='Connect to your Sweetmeter dashboard.'))
        info_path.write_bytes(plistlib.dumps(info))
        write_json_atomic(native / 'Contents/Resources/build-metadata.json', metadata)
        identity = args.sign_identity or '-'
        run('/usr/bin/xattr', '-cr', native)
        run('/usr/bin/codesign', '--force', '--deep', '--sign', identity, *codesign_options(identity), native)
        run('/usr/bin/codesign', '--verify', '--deep', '--strict', native)
        check_designated_requirement(native, identity)
        notarize(native, identity)
    else:
        native = normalized / 'Sweetmeter'
        shutil.copytree(raw, native, symlinks=False)
        write_json_atomic(native / '_internal/build-metadata.json', metadata)
        if sys.platform == 'win32':
            authenticode(native / 'Sweetmeter.exe')
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
