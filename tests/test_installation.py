"""Install repair, startup registration and uninstall in a sandboxed home only."""
import isolation  # noqa: F401  (test sandbox; must be the first import)
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

    def make_app(self, root, marker=b'app', version='2026.9.1'):
        executable = Path(paths.app_command(root)[0])
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(marker)
        if sys.platform == 'darwin':
            (root / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': paths.BUNDLE_ID}))
            (root / 'Contents/Resources').mkdir(parents=True, exist_ok=True)
            (root / 'Contents/Resources/VERSION').write_text(version + '\n')
            helper = root / 'Contents/Frameworks' / self_update.HELPER_NAME
        else:
            (root / '_internal').mkdir(exist_ok=True)
            (root / '_internal/build-metadata.json').write_text(json.dumps({'kind': 'sweetmeter-companion-build'}))
            (root / '_internal/VERSION').write_text(version + '\n')
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
        with patch.object(installation, 'startup') as startup, \
                patch.object(installation, 'startup_registered', return_value=True):
            installation.ensure_registration(destination)
        # The choice is off: an existing entry is removed, never (re)created.
        startup.assert_called_once_with([], enable=False)
        with patch.object(installation, 'startup') as startup:
            installation.ensure_registration(destination)
        startup.assert_not_called()  # Nothing registered: no launchctl/registry work at every start.

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

    def test_repair_rewrites_autostart_entry_but_keeps_its_profile_variables(self):
        # Same rule as the macOS login item: a repair (start_now=False) runs
        # in an app that may lack the user's shell profile. It rewrites the
        # fields this module manages (program, launcher path) but keeps the
        # profile variables the explicit install recorded.
        entry = self.home / 'config/autostart/sweetmeter.desktop'
        with patch.object(sys, 'platform', 'linux'):
            with patch.dict(os.environ, CODEX_HOME='/codex home'):
                installation.startup(['/old-launcher', '--launch'], start_now=True)
            self.assertNotIn('CODEX_HOME', os.environ)
            installation.startup(['/launcher', '--launch'], start_now=False)
            text = entry.read_text()
            self.assertIn('Exec="env" "CODEX_HOME=/codex home" "/launcher" "--launch"\n', text)
            self.assertNotIn('/old-launcher', text)
            self.assertEqual(self.popen.call_count, 1)  # Only the explicit install started it.
            # An entry this module cannot read back is rewritten from the current environment.
            entry.write_text('existing')
            installation.startup(['/launcher', '--launch'], start_now=False)
            self.assertIn('Exec="/launcher" "--launch"\n', entry.read_text())

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

    def test_app_is_moved_aside_before_anything_is_deleted(self):
        destination, _ = self.install()
        seen = []
        real_remove = installation._remove
        def remove(path, removed):
            seen.append((Path(path).name, destination.exists()))
            return real_remove(path, removed)
        with patch.object(installation, 'startup'), patch.object(installation, '_remove', side_effect=remove):
            removed = installation.uninstall()
        self.assertIn(destination, removed)
        deleted = [name for name, _ in seen if '.removing-' in name]
        self.assertEqual(len(deleted), 1)
        self.assertTrue(all(not present for name, present in seen if '.removing-' in name))
        self.assertEqual(installation._sibling_leftovers(destination), [])

    def test_failed_move_deletes_nothing_and_restores_the_login_item(self):
        destination, _ = self.install()
        real_rename = Path.rename
        def rename(path, target):
            if '.removing-' in Path(target).name:
                raise PermissionError('in use')
            return real_rename(path, target)
        with patch.object(Path, 'rename', rename), patch.object(self_update.time, 'sleep'), \
                patch.object(installation, 'startup') as startup:
            with self.assertRaisesRegex(installation.InstallError, 'Nothing was deleted'):
                installation.uninstall()
        startup.assert_not_called()  # Failed before the login item was touched.
        self.assertTrue(Path(paths.app_command(destination)[0]).is_file())
        self.assertTrue((paths.data_dir() / 'install.json').is_file())

    def test_failed_login_item_removal_puts_the_app_back(self):
        destination, _ = self.install()
        with patch.object(installation, 'startup', side_effect=installation.InstallError('launchd')), \
                patch.object(installation, 'startup_registered', return_value=False), \
                patch.object(installation, 'ensure_registration') as register:
            with self.assertRaises(installation.InstallError):
                installation.uninstall()
        self.assertTrue(Path(paths.app_command(destination)[0]).is_file())
        self.assertEqual(installation._sibling_leftovers(destination), [])
        register.assert_not_called()  # Nothing was registered before, so nothing to restore.

    def test_restores_a_removed_login_item_when_the_app_cannot_be_moved_back(self):
        destination, _ = self.install()
        states = iter([True, False])
        with patch.object(installation, 'startup', side_effect=installation.InstallError('launchd')), \
                patch.object(installation, 'startup_registered', side_effect=lambda: next(states)), \
                patch.object(installation, 'ensure_registration') as register:
            with self.assertRaises(installation.InstallError):
                installation.uninstall()
        register.assert_called_once()
        self.assertEqual(register.call_args.args[0], destination)

    def test_undeletable_leftover_is_reported_plainly(self):
        destination, _ = self.install()
        import shutil
        real_rmtree = shutil.rmtree
        def rmtree(path, *args, **kwargs):
            if '.removing-' in Path(path).name:
                raise PermissionError('locked')
            return real_rmtree(path, *args, **kwargs)
        with patch.object(installation, 'startup'), patch.object(self_update.time, 'sleep'), \
                patch.object(installation.shutil, 'rmtree', rmtree):
            with self.assertRaisesRegex(installation.InstallError, 'could not be deleted'):
                installation.uninstall()
        self.assertFalse(destination.exists())  # Never half-deleted under its real name.
        leftovers = installation._sibling_leftovers(destination)
        self.assertEqual(len(leftovers), 1)
        with patch.object(installation, 'startup'):
            installation.uninstall()  # A later run removes it.
        self.assertEqual(installation._sibling_leftovers(destination), [])

    def test_uninstall_refuses_during_an_update_swap(self):
        destination, _ = self.install()
        with self_update._update_lock():
            with patch.object(installation, 'startup') as startup, \
                    patch.object(installation, 'stop_running_app', return_value=True) as stop:
                with self.assertRaisesRegex(installation.InstallError, 'update is being installed'):
                    installation.uninstall()
        startup.assert_not_called()
        stop.assert_not_called()  # The app being health-checked keeps running.
        self.assertTrue(destination.exists())

    def test_interrupted_update_is_finished_before_uninstalling(self):
        destination, state = self.install()
        backup = destination.with_name(destination.name + '.previous-abc123abc123')
        destination.rename(backup)
        self.make_app(destination, b'new')
        (paths.data_dir() / 'companion-swap.json').write_text(json.dumps(dict(
            schema=1, root=str(destination), nonce='abc123abc123', state_dir=str(state),
            version='2026.9.2', stage='new_installed')))
        with patch.object(installation, 'startup'):
            installation.uninstall()
        self.assertFalse(destination.exists())
        self.assertFalse(backup.exists())
        self.assertFalse((paths.data_dir() / 'companion-swap.json').exists())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permission mask')
    def test_uninstall_command_uses_a_private_umask(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        seen = []
        def record(*args, **kwargs):
            current = os.umask(0)
            os.umask(current)
            seen.append(current)
            return []
        with patch.object(installation, 'uninstall', side_effect=record), patch('sys.stdout'):
            installation.uninstall_main([])
        self.assertEqual(seen, [0o077])

    def test_uninstall_main_prints_plain_error(self):
        with patch.object(installation, 'uninstall', side_effect=installation.InstallError('Quit it first.')), \
                patch('sys.stderr') as stderr:
            self.assertEqual(installation.uninstall_main([]), 1)
        self.assertIn('Quit it first.', ''.join(call.args[0] for call in stderr.write.call_args_list))


class InstallerRepairTests(Sandbox):
    """Re-running the installer over an existing managed copy (install.sh/ps1)."""

    def installed(self, version='2026.9.1', marker=b'installed'):
        destination = self.make_app(paths.default_install_root(), marker, version)
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        (paths.data_dir() / 'install.json').write_text(json.dumps(
            {'kind': 'native', 'root': str(destination), 'startup': True}))
        return destination

    def download(self, version='2026.9.2', marker=b'download'):
        return self.make_app(self.root / 'download' / self.name, marker, version)

    def install(self, source):
        messages = []
        with patch.object(installation, 'startup') as startup:
            result = installation.install_native(source, start_at_login=None, report=messages.append)
        return result, messages, startup

    def executable(self, root):
        return Path(paths.app_command(root)[0]).read_bytes()

    def test_older_copy_is_replaced_atomically_then_registered(self):
        destination = self.installed('2026.9.1')
        result, messages, startup = self.install(self.download('2026.9.2'))
        self.assertEqual(result, destination)
        self.assertEqual(self.executable(destination), b'download')
        self.assertEqual(installation.tree_version(destination).as_tuple(), (2026, 9, 2))
        self.assertEqual(messages, ['Updated Sweetmeter 2026.9.1 to 2026.9.2.'])
        # Same journaled swap as updates, fully finished: no journal or siblings left.
        self.assertFalse((paths.data_dir() / 'companion-swap.json').exists())
        self.assertEqual(installation._sibling_leftovers(destination), [])
        self.assertTrue(startup.call_args.kwargs['start_now'])

    def test_same_version_failing_its_self_test_is_replaced(self):
        destination = self.installed('2026.9.2')
        self.run_mock.return_value = Mock(returncode=1, stdout=b'', stderr=b'')
        _, messages, _ = self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'download')
        self.assertIn('Replaced a damaged Sweetmeter installation (it failed its self-test)', messages[0])
        command = self.run_mock.call_args_list[0].args[0]
        self.assertEqual(command[-1], '--self-test')

    def test_same_version_with_damaged_files_is_replaced(self):
        destination = self.installed('2026.9.2')
        (Path(paths.app_command(destination)[0]).parent / 'bad').symlink_to('/etc')
        _, messages, _ = self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'download')
        self.assertIn('its files are damaged', messages[0])

    def test_healthy_same_or_newer_copy_is_kept_and_only_registration_repaired(self):
        for installed_version, note in (('2026.9.2', ''), ('2026.9.5', ' (newer than this package)')):
            with self.subTest(installed=installed_version):
                destination = self.installed(installed_version)
                _, messages, startup = self.install(self.download('2026.9.2'))
                self.assertEqual(self.executable(destination), b'installed')
                self.assertEqual(messages, [f'Sweetmeter {installed_version}{note} is already installed '
                                            'and working; its login startup was checked.'])
                startup.assert_called_once()
                import shutil
                shutil.rmtree(destination)
                shutil.rmtree(self.root / 'download')

    def test_replacement_waits_for_no_update_and_never_runs_during_one(self):
        destination = self.installed('2026.9.1')
        source = self.download('2026.9.2')
        with self_update._update_lock():
            with self.assertRaisesRegex(installation.InstallError, 'update is being installed'):
                self.install(source)
        self.assertEqual(self.executable(destination), b'installed')

    def test_interrupted_replacement_restores_the_previous_copy(self):
        destination = self.installed('2026.9.1')
        source = self.download('2026.9.2')
        real_rename = Path.rename
        def rename(path, target):
            if '.incoming-' in Path(path).name:
                raise PermissionError('in use')
            return real_rename(path, target)
        with patch.object(Path, 'rename', rename), patch.object(self_update.time, 'sleep'):
            with self.assertRaises(PermissionError):
                self.install(source)
        self.assertEqual(self.executable(destination), b'installed')
        self.assertFalse((paths.data_dir() / 'companion-swap.json').exists())
        self.assertEqual(installation._sibling_leftovers(destination), [])

    def test_app_under_an_update_health_check_is_never_stopped(self):
        # An update helper holds the lock while it health-checks the new
        # app: a re-run installer must refuse without stopping that app.
        destination = self.installed('2026.9.1')
        source = self.download('2026.9.2')
        with self_update._update_lock(), \
                patch.object(installation, 'stop_running_app', return_value=True) as stop:
            with self.assertRaisesRegex(installation.InstallError, 'update is being installed'):
                self.install(source)
        stop.assert_not_called()
        self.assertEqual(self.executable(destination), b'installed')
        # Without an update running, the app is stopped only once the lock is held.
        order = []
        real_lock = self_update._update_lock

        def lock():
            order.append('lock')
            return real_lock()
        with patch.object(self_update, '_update_lock', lock), \
                patch.object(installation, 'stop_running_app', side_effect=lambda: order.append('stop') or True):
            self.install(source)
        self.assertEqual(order, ['lock', 'stop'])

    def test_package_without_a_version_never_crashes_the_installer(self):
        destination = self.installed('2026.9.5')
        source = self.download('2026.9.2')
        (source / ('Contents/Resources/VERSION' if sys.platform == 'darwin' else '_internal/VERSION')).unlink()
        _, messages, _ = self.install(source)
        self.assertEqual(self.executable(destination), b'installed')
        self.assertEqual(messages, ['Sweetmeter 2026.9.5 is already installed and working; '
                                    'its login startup was checked.'])

    def test_newer_install_is_never_downgraded_after_a_self_test_timeout(self):
        destination = self.installed('2026.9.5')
        self.run_mock.side_effect = installation.subprocess.TimeoutExpired('Sweetmeter', 1)
        _, messages, startup = self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'installed')
        self.assertIn('was kept', messages[0])
        timeouts = [call.kwargs['timeout'] for call in self.run_mock.call_args_list
                    if call.args and call.args[0][-1] == '--self-test']
        self.assertEqual(timeouts, [installation.SELF_TEST_TIMEOUT, 2 * installation.SELF_TEST_TIMEOUT])
        startup.assert_called_once()

    def test_self_test_that_passes_on_the_longer_retry_keeps_the_install(self):
        destination = self.installed('2026.9.2')
        self.run_mock.side_effect = [installation.subprocess.TimeoutExpired('Sweetmeter', 1),
                                     Mock(returncode=0, stdout=b'', stderr=b'')]
        _, messages, _ = self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'installed')
        self.assertIn('already installed and working', messages[0])

    def test_older_install_whose_self_test_times_out_is_still_updated(self):
        destination = self.installed('2026.9.1')
        self.run_mock.side_effect = installation.subprocess.TimeoutExpired('Sweetmeter', 1)
        self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'download')

    def test_interrupted_installer_swap_is_not_reported_as_an_update_rollback(self):
        destination = self.installed('2026.9.1')
        state = paths.default_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        swap = self_update.TreeSwap(destination, nonce='abc123abc123', state_dir=state, version='2026.9.2',
                                    origin='installer')
        swap.prepare(self.download('2026.9.2'))
        swap._stage('prepared')  # Journal written, as TreeSwap.swap() does first.
        destination.rename(swap.backup)
        swap._stage('old_moved')  # The installer stopped here (power cut, killed).
        self.assertTrue(self_update.recover_update())
        self.assertEqual(self.executable(destination), b'installed')
        result = json.loads((state / 'companion-update-result.json').read_text())
        self.assertEqual(result['status'], 'install_interrupted')

    def test_running_app_is_stopped_before_replacement(self):
        destination = self.installed('2026.9.1')
        with patch.object(installation, 'stop_running_app', return_value=False):
            with self.assertRaisesRegex(installation.InstallError, 'still running'):
                self.install(self.download('2026.9.2'))
        self.assertEqual(self.executable(destination), b'installed')

    def test_installed_copy_checking_itself_reports_damage(self):
        destination = self.installed('2026.9.2')
        (destination / ('Contents/Resources/VERSION' if sys.platform == 'darwin' else '_internal/VERSION')).unlink()
        with self.assertRaisesRegex(installation.InstallError, 'damaged'):
            self.install(destination)

    def test_recorded_start_at_login_choice_survives_a_reinstall(self):
        destination = self.installed('2026.9.1')
        record = json.loads((paths.data_dir() / 'install.json').read_text())
        (paths.data_dir() / 'install.json').write_text(json.dumps(dict(record, startup=False)))
        with patch.object(installation, 'open_installed') as opened, \
                patch.object(installation, 'startup_registered', return_value=True):
            _, _, startup = self.install(self.download('2026.9.2'))
        startup.assert_called_once_with([], enable=False)
        opened.assert_called_once_with(destination)


class StartAtLoginTests(Sandbox):
    def test_checkbox_turns_the_entry_off_and_on_and_is_remembered(self):
        registered = patch.object(installation, 'startup_registered', return_value=True)
        registered.start()
        self.addCleanup(registered.stop)
        destination = self.make_app(paths.default_install_root())
        with patch.object(installation, 'startup'):
            installation.install_native(destination)
        self.assertEqual(installation.start_at_login_choice(), (True, True))
        with patch.object(installation, 'startup') as startup:
            installation.set_start_at_login(False)
        startup.assert_called_once_with([], enable=False)
        self.assertEqual(installation.start_at_login_choice(), (True, False))
        # A later repair from the running app keeps it off.
        with patch.object(installation, 'startup') as startup:
            installation.ensure_registration(destination, start_now=False)
        startup.assert_called_once_with([], enable=False)
        with patch.object(installation, 'startup') as startup:
            installation.set_start_at_login(True)
        self.assertFalse(startup.call_args.kwargs['start_now'])
        self.assertIn('--background', startup.call_args.args[0])
        self.assertEqual(installation.start_at_login_choice(), (True, True))

    def test_source_install_choice_uses_the_recorded_command(self):
        paths.data_dir().mkdir(parents=True)
        (paths.data_dir() / 'install.json').write_text(json.dumps(
            {'kind': 'source', 'root': str(paths.data_dir() / 'runtime'), 'startup': True,
             'command': ['/venv/python', '/runtime/run.py']}))
        with patch.object(installation, 'startup') as startup:
            installation.set_start_at_login(False)
            installation.set_start_at_login(True)
        self.assertEqual(startup.call_args_list[0].args, ([],))
        self.assertEqual(startup.call_args_list[1].args[0][:2], ['/venv/python', '/runtime/run.py'])

    def test_unavailable_without_an_installation(self):
        self.assertEqual(installation.start_at_login_choice()[0], False)
        with self.assertRaises(installation.InstallError):
            installation.set_start_at_login(True)

    @unittest.skipUnless(sys.platform == 'darwin', 'LaunchAgent')
    def test_repair_rewrites_any_changed_field_and_reloads(self):
        agent = self.home / 'Library/LaunchAgents' / (installation.LABEL + '.plist')
        with patch.dict(os.environ, PATH='/opt/homebrew/bin:/usr/bin', CODEX_HOME='/codex'):
            installation.startup(['/launcher', '--launch'], start_now=True)
        self.run_mock.reset_mock()
        # Same program, same everything: nothing is written or reloaded.
        installation.startup(['/launcher', '--launch'], start_now=False)
        self.run_mock.assert_not_called()
        # An older release wrote another ProcessType and output path.
        config = plistlib.loads(agent.read_bytes())
        config.update(ProcessType='Background', StandardOutPath='/old/agent.log')
        agent.write_bytes(plistlib.dumps(config))
        with patch.dict(os.environ, PATH='/usr/bin:/bin'):
            os.environ.pop('CODEX_HOME', None)
            installation.startup(['/launcher', '--launch'], start_now=False)
        config = plistlib.loads(agent.read_bytes())
        self.assertEqual(config['ProcessType'], 'Interactive')
        self.assertIn('launcher-output.log', config['StandardOutPath'])
        self.assertEqual(config['EnvironmentVariables'], {'PATH': '/opt/homebrew/bin:/usr/bin', 'CODEX_HOME': '/codex'})
        commands = [call.args[0][:2] for call in self.run_mock.call_args_list]
        self.assertEqual(commands, [['launchctl', 'bootout'], ['launchctl', 'bootstrap']])

    def test_linux_repair_rewrites_a_damaged_entry_but_keeps_profile_variables(self):
        entry = self.home / 'config/autostart/sweetmeter.desktop'
        with patch.object(sys, 'platform', 'linux'), patch.dict(os.environ, CODEX_HOME='/my codex'):
            installation.startup(['/old launcher', '--launch'], start_now=False)
        self.assertIn('"CODEX_HOME=/my codex"', entry.read_text())
        with patch.object(sys, 'platform', 'linux'):
            os.environ.pop('CODEX_HOME', None)
            installation.startup(['/new launcher', '--launch'], start_now=False)
        text = entry.read_text()
        self.assertIn('"/new launcher"', text)
        self.assertIn('"CODEX_HOME=/my codex"', text)
        entry.write_text('garbage')
        with patch.object(sys, 'platform', 'linux'):
            installation.startup(['/new launcher', '--launch'], start_now=False)
        self.assertIn('Exec="/new launcher" "--launch"', entry.read_text())


if __name__ == '__main__':
    unittest.main()
