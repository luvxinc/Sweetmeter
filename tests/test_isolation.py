"""Every test module runs inside the shared sandbox from isolation.py."""
import isolation  # noqa: F401  (sandbox; must be the first import)
import ast
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# Owned by the Bluetooth engineer, who adds the same first line. Until then
# they still run sandboxed under `discover` (the sandbox is process-wide and is
# active before the first test runs), but not when run on their own.
PENDING_OWNER_EDIT = set()


def first_import(path):
    """Name of the first module a test file imports."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Import):
            return node.names[0].name
        if isinstance(node, ast.ImportFrom) and node.module != '__future__':
            return node.module
    return None


class IsolationTests(unittest.TestCase):
    def test_every_test_module_imports_the_sandbox_first(self):
        modules = sorted(HERE.glob('test_*.py'))
        self.assertGreater(len(modules), 10)
        missing = [path.name for path in modules
                   if first_import(path) != 'isolation' and path.name not in PENDING_OWNER_EDIT]
        self.assertEqual(missing, [], 'these test modules must start with `import isolation`')

    def test_home_and_app_data_are_temporary(self):
        sandbox = os.path.realpath(isolation.SANDBOX)
        for key in ('HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME'):
            self.assertTrue(os.path.realpath(os.environ[key]).startswith(sandbox), key)
        self.assertTrue(os.path.realpath(Path.home()).startswith(sandbox))
        from meter.paths import data_dir, default_install_root
        self.assertTrue(os.path.realpath(data_dir()).startswith(sandbox))
        self.assertTrue(os.path.realpath(default_install_root()).startswith(sandbox))
        for key in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'CLAUDE_CODE_OAUTH_TOKEN'):
            self.assertNotIn(key, os.environ)

    def test_system_services_cannot_be_started(self):
        for command in (['/usr/bin/security', 'find-generic-password', '-s', 'x', '-w'],
                        ['launchctl', 'bootout', 'gui/0/x'], ['/usr/bin/open', '-a', 'x'],
                        ['reg.exe', 'query', 'HKCU'], ['osascript', '-e', '1'], ['systemctl', 'status'],
                        ['codex', 'app-server'], 'security find-generic-password'):
            with self.subTest(command=command), self.assertRaises(isolation.IsolationViolation):
                subprocess.run(command, capture_output=True, shell=isinstance(command, str))

    def test_product_code_cannot_swallow_a_violation(self):
        # claude_credentials catches OSError; the violation must still surface.
        from meter import providers
        with patch.object(providers.sys, 'platform', 'darwin'):
            with self.assertRaises(isolation.IsolationViolation):
                providers.claude_credentials()

    def test_an_explicit_patch_still_works(self):
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            subprocess.run(['launchctl', 'list'])
        run.assert_called_once()

    def test_ordinary_programs_still_run(self):
        import sys
        result = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), '1')


if __name__ == '__main__':
    unittest.main()
