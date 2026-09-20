"""Optional release-runner Developer ID keychain; never used by PR workflows."""
import argparse
import base64
import os
from pathlib import Path
import secrets
import subprocess
import sys


def run(*arguments):
    result = subprocess.run(arguments, capture_output=True)
    if result.returncode:
        # Never include command arguments or security's potentially sensitive output.
        raise RuntimeError('macOS signing keychain operation failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('macOS signing setup is macOS-only')
    temporary = Path(os.environ['RUNNER_TEMP'])
    keychain = temporary / 'sweetmeter-signing.keychain-db'
    certificate = temporary / 'sweetmeter-developer-id.p12'
    if args.cleanup:
        if keychain.exists():
            run('security', 'delete-keychain', str(keychain))
        certificate.unlink(missing_ok=True)
        return
    values = [os.environ.get(key, '') for key in ('MACOS_CERTIFICATE_P12', 'MACOS_CERTIFICATE_PASSWORD', 'MACOS_SIGN_IDENTITY')]
    if not any(values):
        print('No Developer ID configured: ad-hoc build, not notarized.')
        return
    if not all(values):
        raise SystemExit('All three optional Developer ID secrets must be configured together')
    raw, certificate_password, identity = values
    descriptor = os.open(certificate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(base64.b64decode(raw, validate=True))
    password = secrets.token_urlsafe(32)
    try:
        run('security', 'create-keychain', '-p', password, str(keychain))
        run('security', 'set-keychain-settings', '-lut', '21600', str(keychain))
        run('security', 'unlock-keychain', '-p', password, str(keychain))
        run('security', 'import', str(certificate), '-k', str(keychain), '-P', certificate_password,
            '-T', '/usr/bin/codesign')
        run('security', 'set-key-partition-list', '-S', 'apple-tool:,apple:', '-k', password, str(keychain))
        run('security', 'list-keychains', '-d', 'user', '-s', str(keychain))
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write('MACOS_SIGN_IDENTITY=' + identity + '\n')
    finally:
        certificate.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
