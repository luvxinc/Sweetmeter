"""Install repair, startup registration and uninstall in a sandboxed home only."""
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from meter import installation, paths, self_update

REAL_REGISTER_UNINSTALL = installation.register_uninstall_entry


class Sandbox(unittest.TestCase):
    """Every path (HOME, XDG, LOCALAPPDATA, candidate app roots) is temporary."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        environment = patch.dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                                 XDG_DATA_HOME=str(self.home / 'data'), XDG_CONFIG_HOME=str(self.home / 'config'),
                                 LOCALAPPDATA=str(self.home / 'local'), APPDATA=str(self.home / 'roaming'))
        environment.start()
        self.addCleanup(environment.stop)
        self.assertTrue(str(paths.data_dir()).startswith(str(self.home)))
        self.system_root = self.root / 'SystemApplications' / self.name
        roots = patch.object(installation, 'candidate_install_roots',
                             return_value=[paths.default_install_root(), self.system_root])
        roots.start()
        self.addCleanup(roots.stop)
        # Never write the real Windows registry from tests.
        for name in ('register_uninstall_entry', '_remove_uninstall_entry'):
            registry = patch.object(installation, name)
            registry.start()
            self.addCleanup(registry.stop)
        for module in (installation, self_update):
            runner = patch.object(module.subprocess, 'run', return_value=Mock(returncode=0, stdout='', stderr=''))
            launcher = patch.object(module.subprocess, 'Popen')
            self.run_mock = runner.start()
            self.popen = launcher.start()
            self.addCleanup(runner.stop)
            self.addCleanup(launcher.stop)

    @property
    def name(self):
        return 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'

    def make_app(self, root, marker=b'app'):
        executable = Path(paths.app_command(root)[0])
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(marker)
        if sys.platform == 'darwin':
            (root / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': paths.BUNDLE_ID}))
            helper = root / 'Contents/Frameworks' / self_update.HELPER_NAME
        else:
            (root / '_internal').mkdir(exist_ok=True)
            (root / '_internal/build-metadata.json').write_text(json.dumps({'kind': 'sweetmeter-companion-build'}))
            helper = root / '_internal' / self_update.HELPER_NAME
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b'launcher ' + marker)
        return root


class RepairTests(Sandbox):
    def test_crash_before_registration_is_repaired_on_rerun(self):
        destination = self.make_app(paths.default_install_root())
        source = self.make_app(self.root / 'download' / self.name, b'download')
        with patch.object(installation, 'startup') as startup:
            self.assertEqual(installation.install_native(source), destination)
        record = json.loads((paths.data_dir() / 'install.json').read_text())
        self.assertEqual(record, {'kind': 'native', 'root': str(destination), 'startup': True})
        launcher = paths.data_dir() / 'launcher' / self_update.LAUNCHER_NAME
        self.assertEqual(launcher.read_bytes(), b'launcher app')  # From the installed app, not the download.
        self.assertEqual(Path(paths.app_command(destination)[0]).read_bytes(), b'app')
        startup.assert_called_once()
        self.assertTrue(startup.call_args.kwargs['start_now'])
        self.assertEqual(startup.call_args.args[0][:2], [str(launcher), '--launch'])

    def test_running_manual_copy_registers_without_starting_again(self):
        destination = self.make_app(paths.default_install_root())
        executable = Path(paths.app_command(destination)[0])
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(executable)), \
                patch.object(installation, 'startup') as startup:
            self.assertTrue(installation.repair_running_installation())
        self.assertFalse(startup.call_args.kwargs['start_now'])
        self.assertTrue((paths.data_dir() / 'install.json').is_file())

    def test_repair_respects_recorded_no_startup_choice(self):
        destination = self.make_app(paths.default_install_root())
        paths.data_dir().mkdir(parents=True)
        (paths.data_dir() / 'install.json').write_text(json.dumps(
            {'kind': 'native', 'root': str(destination), 'startup': False}))
        with patch.object(installation, 'startup') as startup:
            installation.ensure_registration(destination)
        startup.assert_not_called()

    def test_pending_update_never_replaces_the_launcher(self):
        destination = self.make_app(paths.default_install_root())
        launcher = paths.data_dir() / 'launcher' / self_update.LAUNCHER_NAME
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b'known good launcher')
        executable = Path(paths.app_command(destination)[0])
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(executable)), \
                patch.dict(os.environ, SWEETMETER_UPDATE_HEALTH=str(self.root / 'healthy.json')), \
                patch.object(installation, 'startup'):
            installation.repair_running_installation()
        self.assertEqual(launcher.read_bytes(), b'known good launcher')

    def test_app_in_system_applications_is_adopted_not_duplicated(self):
        source = self.make_app(self.system_root)
        with patch.object(installation, 'startup'):
            self.assertEqual(installation.install_native(source), source)
        self.assertFalse(paths.default_install_root().exists())
        self.assertEqual(json.loads((paths.data_dir() / 'install.json').read_text())['root'], str(source))
        with patch.object(paths, 'candidate_install_roots', return_value=[paths.default_install_root(), source]):
            self.assertEqual(paths.install_root(), source)

    def test_recorded_copy_wins_over_second_copy(self):
        recorded = self.make_app(paths.default_install_root(), b'recorded')
        with patch.object(installation, 'startup'):
            installation.install_native(recorded)
            other = self.make_app(self.system_root, b'other')
            self.assertEqual(installation.install_native(other), recorded)
        self.assertEqual(json.loads((paths.data_dir() / 'install.json').read_text())['root'], str(recorded))

    def test_concurrent_installer_gets_plain_error(self):
        source = self.make_app(self.root / 'download' / self.name)
        errors = []
        def other():
            try:
                installation.install_native(source)
            except installation.InstallError as error:
                errors.append(str(error))
        with installation.install_lock(), patch.object(installation, 'LOCK_TIMEOUT', .3), \
                patch.object(installation, 'startup'):
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(10)
        self.assertEqual(len(errors), 1)
        self.assertIn('Another Sweetmeter setup is still running', errors[0])
        self.assertFalse(paths.default_install_root().exists())


class StartupTests(Sandbox):
    @unittest.skipUnless(sys.platform == 'darwin', 'LaunchAgent')
    def test_launch_agent_output_does_not_duplicate_agent_log(self):
        installation.startup(['/launcher', '--launch'], start_now=False)
        plist = plistlib.loads((self.home / 'Library/LaunchAgents' / (installation.LABEL + '.plist')).read_bytes())
        self.assertNotIn('agent.log', plist['StandardOutPath'])
        self.assertEqual(plist['StandardOutPath'], plist['StandardErrorPath'])
        self.run_mock.assert_not_called()  # start_now=False does not touch launchctl.

    def test_windows_run_key_replaces_vbscript(self):
        values, deleted = {}, []

        class Key:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        def query(key, name):
            if name not in values:
                raise FileNotFoundError(name)
            return values[name], 1
        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER='HKCU', KEY_SET_VALUE=2, REG_SZ=1, REG_DWORD=4, QueryValueEx=query,
            CreateKeyEx=lambda *a: Key(), OpenKey=lambda *a: Key(),
            SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, value),
            DeleteValue=lambda key, name: deleted.append(name), DeleteKey=lambda *a: deleted.append('key'))
        legacy = self.home / 'roaming/Microsoft/Windows/Start Menu/Programs/Startup/Sweetmeter.vbs'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('old')
        command = [str(self.home / 'Local Data/Ünicode/sweetmeter-launcher.exe'), '--launch', '--background']
        with patch.object(sys, 'platform', 'win32'), patch.dict(sys.modules, winreg=fake), \
                patch.dict(os.environ, CODEX_HOME='C:\\Codex Home'):
            installation.startup(command, start_now=False)
            self.assertFalse(legacy.exists())
            self.assertEqual(values['Sweetmeter'], installation.subprocess.list2cmdline(command))
            environment = json.loads((self.home / 'local/Sweetmeter/startup-environment.json').read_text())
            self.assertEqual(environment, {'CODEX_HOME': 'C:\\Codex Home'})
            REAL_REGISTER_UNINSTALL(self.home / 'app', self.home / 'launcher.exe')
            self.assertIn('--uninstall', values['UninstallString'])
            installation.startup([], enable=False)
        self.assertEqual(deleted, ['Sweetmeter'])

    @unittest.skipUnless(sys.platform == 'darwin', 'LaunchAgent')
    def test_repair_from_finder_keeps_installer_path(self):
        with patch.dict(os.environ, PATH='/opt/homebrew/bin:/usr/bin', CODEX_HOME='/codex'):
            installation.startup(['/old-launcher', '--launch'], start_now=True)
        with patch.dict(os.environ, PATH='/usr/bin:/bin'):
            os.environ.pop('CODEX_HOME', None)
            installation.startup(['/launcher', '--launch'], start_now=False)
        plist = plistlib.loads((self.home / 'Library/LaunchAgents' / (installation.LABEL + '.plist')).read_bytes())
        self.assertEqual(plist['ProgramArguments'], ['/launcher', '--launch'])
        self.assertEqual(plist['EnvironmentVariables'], {'PATH': '/opt/homebrew/bin:/usr/bin', 'CODEX_HOME': '/codex'})

    @unittest.skipIf(sys.platform in ('darwin', 'win32'), 'XDG autostart')
    def test_repair_keeps_existing_autostart_entry(self):
        entry = self.home / 'config/autostart/sweetmeter.desktop'
        entry.parent.mkdir(parents=True)
        entry.write_text('existing')
        installation.startup(['/launcher', '--launch'], start_now=False)
        self.assertEqual(entry.read_text(), 'existing')
        installation.startup(['/launcher', '--launch'], start_now=True)
        self.assertIn('/launcher', entry.read_text())

    def test_line_breaks_are_rejected_before_writing(self):
        with self.assertRaises(installation.InstallError):
            installation.startup(['/launcher\n--evil'], start_now=False)


class UninstallTests(Sandbox):
    def install(self):
        destination = self.make_app(paths.default_install_root())
        with patch.object(installation, 'startup'):
            installation.install_native(destination)
        state = paths.default_state_dir()
        (state / 'companion.json').write_text('identity')
        return destination, state

    def test_uninstall_keeps_data_by_default_and_removes_only_managed_paths(self):
        destination, state = self.install()
        stranger = self.system_root.parent / 'Unrelated.app'
        stranger.mkdir(parents=True)
        stale = destination.with_name(destination.name + '.previous-0123456789ab')
        stale.mkdir()
        with patch.object(installation, 'startup') as startup:
            removed = installation.uninstall()
        startup.assert_called_once_with([], enable=False)
        self.assertFalse(destination.exists())
        self.assertFalse(stale.exists())
        self.assertFalse((paths.data_dir() / 'install.json').exists())
        self.assertFalse((paths.data_dir() / 'launcher').exists())
        self.assertEqual((state / 'companion.json').read_text(), 'identity')
        self.assertTrue(stranger.exists())
        self.assertIn(destination, removed)

    def test_remove_data_deletes_state(self):
        _, state = self.install()
        with patch.object(installation, 'startup'):
            installation.uninstall(remove_data=True)
        self.assertFalse(state.exists())
        self.assertFalse(paths.data_dir().exists())

    def test_unrecognized_folder_at_default_location_is_kept(self):
        foreign = paths.default_install_root()
        foreign.mkdir(parents=True)
        (foreign / 'keep').write_text('keep')
        with patch.object(installation, 'startup'):
            installation.uninstall()
        self.assertTrue((foreign / 'keep').exists())

    def test_running_app_is_stopped_first(self):
        from meter.instance_lock import InstanceLock
        self.install()
        state = paths.default_state_dir()
        lock = InstanceLock(state / 'meter.lock')
        (state / 'meter.pid').write_text('424242')
        def terminate(pid, timeout):
            self.assertEqual(pid, 424242)
            lock.close()
            return True
        with patch.object(self_update, 'terminate_pid', side_effect=terminate), \
                patch.object(installation, 'startup'):
            installation.uninstall()
        self.assertFalse(paths.default_install_root().exists())

    def test_app_that_cannot_be_stopped_is_left_installed(self):
        from meter.instance_lock import InstanceLock
        destination, state = self.install()
        lock = InstanceLock(state / 'meter.lock')
        self.addCleanup(lock.close)
        with patch.object(installation, 'stop_running_app', return_value=False):
            with self.assertRaises(installation.InstallError):
                installation.uninstall()
        self.assertTrue(destination.exists())

    def test_uninstall_main_prints_plain_error(self):
        with patch.object(installation, 'uninstall', side_effect=installation.InstallError('Quit it first.')), \
                patch('sys.stderr') as stderr:
            self.assertEqual(installation.uninstall_main([]), 1)
        self.assertIn('Quit it first.', ''.join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
