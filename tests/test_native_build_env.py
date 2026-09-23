from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    import build_companion
from scripts.setup_native_env import crypto_options


class NativeBuildEnvironmentTests(unittest.TestCase):
    def test_intel_requires_static_libraries_and_preserves_dependency_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            (prefix / 'lib').mkdir()
            env = {}
            with patch('scripts.setup_native_env.subprocess.check_output', return_value=folder + '\n'):
                with self.assertRaisesRegex(RuntimeError, 'static libraries'):
                    crypto_options(env, system='darwin', machine='x86_64')
                for name in ('libssl.a', 'libcrypto.a'):
                    (prefix / 'lib' / name).touch()
                self.assertEqual(crypto_options(env, system='darwin', machine='x86_64'), ['--no-binary=cryptography'])
            self.assertEqual(env, {'OPENSSL_STATIC': '1', 'OPENSSL_DIR': folder})

    def test_other_platforms_require_crypto_wheels(self):
        with patch('scripts.setup_native_env.subprocess.check_output') as brew:
            for system, arch in [('darwin', 'arm64'), ('win32', 'AMD64'), ('linux', 'x86_64')]:
                env = {}
                self.assertEqual(crypto_options(env, system=system, machine=arch), ['--only-binary=cryptography'])
                self.assertFalse(env)
            brew.assert_not_called()

    def test_signing_identity_kinds_and_hardened_runtime(self):
        self.assertEqual(build_companion.signing_kind(None), 'adhoc')
        self.assertEqual(build_companion.signing_kind('-'), 'adhoc')
        self.assertEqual(build_companion.signing_kind('Sweetmeter Code Signing'), 'stable')
        developer = 'Developer ID Application: Example (TEAMID1234)'
        self.assertEqual(build_companion.signing_kind(developer), 'developer-id')
        self.assertEqual(build_companion.codesign_options('Sweetmeter Code Signing'), [])
        options = build_companion.codesign_options(developer)
        self.assertIn('runtime', options)
        self.assertIn(str(build_companion.ENTITLEMENTS), options)
        self.assertTrue(build_companion.ENTITLEMENTS.is_file())

    def test_stable_identity_must_not_yield_cdhash_requirement(self):
        with patch.object(build_companion, 'designated_requirement', return_value='cdhash H"abc"'):
            with self.assertRaises(SystemExit):
                build_companion.check_designated_requirement(Path('/app'), 'Sweetmeter Code Signing')
            build_companion.check_designated_requirement(Path('/app'), '-')  # Ad-hoc: warning only.
        stable = 'identifier "com.sweetmeter.companion" and certificate leaf = H"0123"'
        with patch.object(build_companion, 'designated_requirement', return_value=stable):
            self.assertEqual(build_companion.check_designated_requirement(Path('/app'), 'Sweetmeter Code Signing'), stable)

    def test_notarization_and_authenticode_only_with_complete_secrets(self):
        empty = {key: '' for key in ('APPLE_NOTARY_KEY', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER',
                                     'WINDOWS_CERTIFICATE_PFX', 'WINDOWS_CERTIFICATE_PASSWORD')}
        with patch.dict(build_companion.os.environ, empty), patch.object(build_companion, 'run') as run:
            self.assertFalse(build_companion.notarize(Path('/app'), 'Developer ID Application: X (T)'))
            self.assertFalse(build_companion.authenticode(Path('app.exe')))
        run.assert_not_called()
        with patch.dict(build_companion.os.environ, dict(empty, APPLE_NOTARY_KEY_ID='id')):
            with self.assertRaises(SystemExit):
                build_companion.notarize(Path('/app'), 'Developer ID Application: X (T)')
        with patch.dict(build_companion.os.environ, dict(empty, APPLE_NOTARY_KEY='a2V5', APPLE_NOTARY_KEY_ID='i',
                                                         APPLE_NOTARY_ISSUER='j')):
            with self.assertRaisesRegex(SystemExit, 'Developer ID'):
                build_companion.notarize(Path('/app'), 'Sweetmeter Code Signing')
        with patch.dict(build_companion.os.environ, dict(empty, WINDOWS_CERTIFICATE_PASSWORD='secret')):
            with self.assertRaises(SystemExit):
                build_companion.authenticode(Path('app.exe'))

    def test_signing_environment_is_minimal_and_hash_pinned(self):
        lines = (ROOT / 'packaging/release-signing-requirements.txt').read_text().splitlines()
        requirements = [line.split('==')[0] for line in lines if line and not line[0] in '# ']
        self.assertEqual(requirements, ['cryptography', 'cffi', 'pycparser'])
        pinned = (ROOT / 'requirements.txt').read_text()
        version = next(line for line in lines if line.startswith('cryptography==')).split()[0]
        self.assertIn(version, pinned)
        self.assertTrue(all('--hash=sha256:' in line for line in lines if line.startswith('    ')))

    def test_intel_linkage_check_rejects_collision_before_packaging(self):
        with patch.object(build_companion.sys, 'platform', 'darwin'), patch.object(build_companion.platform, 'machine', return_value='x86_64'):
            with patch.object(build_companion.subprocess, 'check_output', return_value='module:\n /usr/local/lib/libssl.3.dylib (compatibility 3)\n'):
                with self.assertRaisesRegex(RuntimeError, 'statically'):
                    build_companion.check_crypto_linkage()
            with patch.object(build_companion.subprocess, 'check_output', return_value='module:\n /usr/lib/libSystem.B.dylib (compatibility 1)\n'):
                build_companion.check_crypto_linkage()
