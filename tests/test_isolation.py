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

    def test_other_launch_paths_and_wrappers_are_guarded(self):
        import sys
        attempts = [
            lambda: os.system('security find-generic-password -s x'),
            lambda: os.system('true; launchctl list'),
            lambda: subprocess.run(['env', 'A=1', '/usr/bin/security', 'dump-keychain']),
            lambda: subprocess.run(['/bin/sh', '-c', 'launchctl bootout gui/0/x']),
            lambda: subprocess.run(['nohup', 'open', '-a', 'x']),
            lambda: subprocess.run('cd /; codex app-server', shell=True),
            lambda: os.popen('osascript -e 1'),
            lambda: os.execv('/bin/launchctl', ['launchctl', 'list']),
            lambda: os.execvp('sh', ['sh', '-c', 'systemctl status']),
            lambda: os.execl('/usr/bin/env', 'env', 'codex', 'login'),
        ]
        if hasattr(os, 'posix_spawn'):
            attempts += [lambda: os.posix_spawn('/usr/bin/security', ['security', 'list'], dict(os.environ)),
                         lambda: os.posix_spawnp('env', ['env', 'launchctl', 'list'], dict(os.environ))]
        if hasattr(os, 'spawnv'):
            attempts.append(lambda: os.spawnv(os.P_WAIT, '/usr/bin/open', ['open', '-a', 'x']))
        for number, attempt in enumerate(attempts):
            with self.subTest(number=number), self.assertRaises(isolation.IsolationViolation):
                attempt()
        # Words that merely mention a blocked name in data are not programs.
        self.assertIsNone(isolation.blocked_program([sys.executable, '-c', 'open("x")']))
        self.assertIsNone(isolation.blocked_program(['tar', '-cf', 'open', 'security']))
        self.assertEqual(isolation.blocked_program(['sh', '-c', 'echo; "/usr/bin/open" -a x']), 'open')

    def test_non_loopback_network_is_refused_unless_opted_in(self):
        import socket
        import requests
        for host in ('203.0.113.7', 'api.anthropic.com'):
            with self.subTest(host=host), self.assertRaises(isolation.IsolationViolation):
                socket.create_connection((host, 443), timeout=1)
        with self.assertRaises(isolation.IsolationViolation):
            requests.get('https://api.github.com/', timeout=1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, \
                self.assertRaises(isolation.IsolationViolation):
            udp.sendto(b'x', ('198.51.100.1', 53))
        # Loopback still works (local fake servers).
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2):
                pass
        with isolation.allow_network(), patch.object(isolation, '_real_connect') as connect:
            with socket.socket() as client:
                client.connect(('203.0.113.7', 443))
            connect.assert_called_once()
        self.assertFalse(isolation.network_allowed())

    def test_registry_writes_are_refused_on_windows(self):
        import sys
        if sys.platform != 'win32':
            self.skipTest('winreg exists only on Windows')
        import winreg
        with self.assertRaises(isolation.IsolationViolation):
            winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r'Software\SweetmeterTest', 0, winreg.KEY_SET_VALUE)

    def test_ordinary_programs_still_run(self):
        import sys
        result = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), '1')


if __name__ == '__main__':
    unittest.main()
