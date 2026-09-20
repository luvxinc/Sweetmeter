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

    def test_intel_linkage_check_rejects_collision_before_packaging(self):
        with patch.object(build_companion.sys, 'platform', 'darwin'), patch.object(build_companion.platform, 'machine', return_value='x86_64'):
            with patch.object(build_companion.subprocess, 'check_output', return_value='module:\n /usr/local/lib/libssl.3.dylib (compatibility 3)\n'):
                with self.assertRaisesRegex(RuntimeError, 'statically'):
                    build_companion.check_crypto_linkage()
            with patch.object(build_companion.subprocess, 'check_output', return_value='module:\n /usr/lib/libSystem.B.dylib (compatibility 1)\n'):
                build_companion.check_crypto_linkage()
