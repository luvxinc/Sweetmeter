import isolation  # noqa: F401  (test sandbox; must be the first import)
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from meter import self_update as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        # Unpatched path lookups must never reach the real user's installation.
        home = self.root / 'home'
        sandbox = patch.dict(os.environ, HOME=str(home), USERPROFILE=str(home), XDG_DATA_HOME=str(home / 'data'),
                             LOCALAPPDATA=str(home / 'local'), APPDATA=str(home / 'roaming'))
        sandbox.start()
        self.addCleanup(sandbox.stop)
        # Process lookup by tree (ps / /proc) is exercised by its own tests;
        # here Popen is a fake, so no process runs from any tree.
        pids = patch.object(update, 'tree_pids', return_value=set())
        self.tree_pids = pids.start()
        self.addCleanup(pids.stop)
        self.package = self.root / 'package.zip'
        self.name = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
        self.relative_exe = ('Contents/MacOS/Sweetmeter' if sys.platform == 'darwin'
                             else 'Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter')

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self):
        # A package for this computer as the product itself detects it (on
        # Windows that includes PROCESSOR_ARCHITEW6432 and x64 emulation on
        # Arm), rather than a second, simplified copy of that detection.
        choices = update.compatible_platforms()
        self.assertTrue(choices, 'Unrecognized host architecture %r' % platform.machine())
        os_name, arch = choices[0]
        return dict(kind='companion', version='2026.9.2', os=os_name, arch=arch,
                    size=self.package.stat().st_size,
                    sha256=hashlib.sha256(self.package.read_bytes()).hexdigest())

    def write_package(self, entries=None):
        with zipfile.ZipFile(self.package, 'w') as archive:
            for name, content in entries or [(self.name + '/' + self.relative_exe, b'new app')]:
                # ZipInfo(name) normalizes backslashes on Windows. Preserve the
                # raw bytes so malicious-name fixtures are identical on all OSes.
                entry = zipfile.ZipInfo()
                entry.filename = entry.orig_filename = name
                archive.writestr(entry, content)

    def test_source_mode_offers_verified_manual_folder(self):
        self.write_package()
        with patch.object(update, 'install_root', return_value=self.root / self.name), \
                patch.object(update, 'data_dir', return_value=self.root):
            result = update.stage_update(self.package, self.artifact(), self.state)
        self.assertFalse(result.supported)
        self.assertEqual((result.manual_path / self.relative_exe).read_bytes(), b'new app')
        with self.assertRaises(RuntimeError):
            result.launch()

    def test_wrong_hash_or_platform_is_rejected_before_extraction(self):
        self.write_package()
        for field, value in [('sha256', '0' * 64), ('os', 'unknown'), ('size', 1)]:
            artifact = self.artifact()
            artifact[field] = value
            with self.assertRaises(ValueError):
                update.stage_update(self.package, artifact, self.state)
        self.assertFalse((self.state / 'updates').exists())

    def test_zip_traversal_links_duplicates_and_windows_aliases(self):
        for name in ['../escape', '/escape', 'Sweetmeter/../escape', 'C:/escape',
                     'Sweetmeter\\escape', 'Sweetmeter/CON', 'Sweetmeter/a.']:
            with self.subTest(name=name):
                self.write_package([(name, b'bad')])
                with zipfile.ZipFile(self.package) as archive:
                    self.assertEqual(archive.infolist()[0].orig_filename, name)
                with self.assertRaises(ValueError):
                    update.extract_package(self.package, self.root / 'extract')
        with zipfile.ZipFile(self.package, 'w') as archive:
            link = zipfile.ZipInfo('Sweetmeter/link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '../../outside')
        with self.assertRaises(ValueError):
            update.extract_package(self.package, self.root / 'extract')
        self.write_package([('Sweetmeter/A', b'a'), ('Sweetmeter/a', b'b')])
        with self.assertRaises(ValueError):
            update.extract_package(self.package, self.root / 'extract')

    def plan(self):
        installed = self.root / self.name
        executable = installed / self.relative_exe
        executable.parent.mkdir(parents=True)
        executable.write_text('old app')
        staging = self.state / 'updates/test'
        candidate = staging / 'package' / self.name
        target = candidate / self.relative_exe
        target.parent.mkdir(parents=True)
        target.write_text('new app')
        plan = dict(schema=1, root=str(installed), candidate=str(candidate), parent_pid=123,
                    state_dir=str(self.state), version='2026.9.2', nonce='abc123', health_timeout=10,
                    bluetooth_baseline='ok')
        plan_path = staging / 'plan.json'
        plan_path.write_text(json.dumps(plan))
        (self.root / 'install.json').write_text(json.dumps({'root': str(installed), 'kind': 'native'}))
        return installed, plan, plan_path

    def link_package(self, links, extra=None):
        with zipfile.ZipFile(self.package, 'w') as archive:
            archive.writestr('Sweetmeter.app/Contents/Resources/VERSION', '2026.9.2')
            for name, target in links.items():
                link = zipfile.ZipInfo(name)
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, target)
            for name, data in extra or []:
                archive.writestr(name, data)

    @unittest.skipIf(sys.platform == 'win32', 'Windows native packages contain no macOS framework links')
    def test_internal_macos_link_is_preserved(self):
        self.link_package({'Sweetmeter.app/Contents/Frameworks/VERSION': '../Resources/VERSION'})
        destination = self.root / 'extract'
        update.extract_package(self.package, destination)
        link = destination / 'Sweetmeter.app/Contents/Frameworks/VERSION'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), '2026.9.2')
        update.validate_tree(destination / 'Sweetmeter.app')

    def test_macos_escape_cycle_dangling_and_link_parent_are_rejected(self):
        cases = [
            ({'Sweetmeter.app/link': '../outside'}, None),
            ({'Sweetmeter.app/link': '/etc/passwd'}, None),
            ({'Sweetmeter.app/link': 'missing'}, None),
            ({'Sweetmeter.app/a': 'b', 'Sweetmeter.app/b': 'a'}, None),
            ({'Sweetmeter.app/Contents/a': 'Resources'}, [('Sweetmeter.app/Contents/a/injected', 'bad')]),
            ({'Sweetmeter.app/Contents/link': 'resources/VERSION'}, None),
        ]
        for links, extra in cases:
            self.link_package(links, extra)
            with self.assertRaises(ValueError):
                update.extract_package(self.package, self.root / 'extract')
        self.assertFalse((self.root / 'extract').exists())

    def test_success_requires_matching_new_process_health(self):
        installed, plan, plan_path = self.plan()
        real_fsync = os.fsync
        def windows_fsync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                # A zero-byte write changes no content but rejects read-only
                # descriptors, reproducing Windows CRT flush requirements.
                os.write(descriptor, b'')
            real_fsync(descriptor)
        def launch(*args, **kwargs):
            Path(kwargs['env']['SWEETMETER_UPDATE_HEALTH']).write_text(json.dumps(
                dict(version=plan['version'], nonce=plan['nonce'], pid=444)))
            return Process()
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', side_effect=lambda pid: pid == 444), \
                patch.object(update.os, 'fsync', side_effect=windows_fsync), \
                patch.object(update.subprocess, 'Popen', side_effect=launch) as popen:
            update.apply_update(plan_path)
        self.assertEqual((installed / self.relative_exe).read_text(), 'new app')
        self.assertEqual(json.loads((self.state / 'companion-update-result.json').read_text())['status'], 'success')
        self.assertFalse(list(self.root.glob('*.previous-*')))
        self.assertFalse((self.root / 'companion-swap.json').exists())
        environment = popen.call_args.kwargs['env']
        self.assertEqual(environment['SWEETMETER_UPDATE_BLUETOOTH'], 'ok')
        if sys.platform == 'darwin':
            command = popen.call_args.args[0]
            self.assertEqual(command[:2], ['/usr/bin/open', '-n'])
            self.assertIn('-W', command)  # `open` lives as long as the app, so exits are seen.
            self.assertIn('SWEETMETER_UPDATE_NONCE=' + plan['nonce'], command)
        if sys.platform == 'win32':
            self.assertTrue(popen.call_args.kwargs['creationflags'] & update.CREATE_NO_WINDOW)

    def test_windows_normalization_cannot_hide_archive_backslash(self):
        self.write_package([('Sweetmeter\\escape', b'bad')])
        # Simulate only ZipInfo's platform separator, without changing pathlib
        # or the host filesystem, so this regression also runs on macOS/Linux.
        with patch.object(zipfile.os, 'sep', '\\'):
            with zipfile.ZipFile(self.package) as archive:
                entry = archive.infolist()[0]
                self.assertEqual(entry.filename, 'Sweetmeter/escape')
                self.assertEqual(entry.orig_filename, 'Sweetmeter\\escape')
            with self.assertRaises(ValueError):
                update.extract_package(self.package, self.root / 'extract')
        self.assertFalse((self.root / 'extract').exists())

    def test_failed_start_restores_previous_package(self):
        installed, plan, plan_path = self.plan()
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update.subprocess, 'Popen', return_value=Process(exit_code=1)) as launch:
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                update.apply_update(plan_path)
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')
        self.assertEqual(launch.call_count, 2)
        self.assertNotIn('SWEETMETER_UPDATE_HEALTH', launch.call_args.kwargs['env'])
        self.assertEqual(json.loads((self.state / 'companion-update-result.json').read_text())['status'], 'rollback')
        self.assertFalse((self.root / 'companion-swap.json').exists())

    def launch_healthy(self, plan):
        def launch(*args, **kwargs):
            if 'SWEETMETER_UPDATE_HEALTH' in kwargs['env']:
                Path(kwargs['env']['SWEETMETER_UPDATE_HEALTH']).write_text(json.dumps(
                    dict(version=plan['version'], nonce=plan['nonce'], pid=444)))
            return Process()
        return launch

    def test_locked_old_app_is_restarted_when_it_cannot_be_moved(self):
        installed, plan, plan_path = self.plan()
        real_rename = Path.rename
        def rename(path, target):
            if Path(path) == installed:
                raise PermissionError('in use by antivirus')
            return real_rename(path, target)
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update.time, 'sleep'), \
                patch.object(Path, 'rename', rename), \
                patch.object(update.subprocess, 'Popen', return_value=Process()) as launch:
            with self.assertRaises(PermissionError):
                update.apply_update(plan_path)
        # Retried with backoff, then the untouched previous app is started again.
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')
        launch.assert_called_once()
        self.assertNotIn('SWEETMETER_UPDATE_HEALTH', launch.call_args.kwargs['env'])
        result = json.loads((self.state / 'companion-update-result.json').read_text())
        self.assertEqual(result['status'], 'rollback')
        self.assertFalse((self.root / 'companion-swap.json').exists())
        self.assertFalse(list(self.root.glob('*.incoming-*')))

    def test_transient_rename_lock_is_retried(self):
        installed, plan, plan_path = self.plan()
        real_rename = Path.rename
        failures = []
        def rename(path, target):
            if Path(path) == installed and not failures:
                failures.append(path)
                raise PermissionError('briefly locked')
            return real_rename(path, target)
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', side_effect=lambda pid: pid == 444), \
                patch.object(update.time, 'sleep'), \
                patch.object(Path, 'rename', rename), \
                patch.object(update.subprocess, 'Popen', side_effect=self.launch_healthy(plan)):
            update.apply_update(plan_path)
        self.assertEqual(failures, [installed])
        self.assertEqual((installed / self.relative_exe).read_text(), 'new app')

    def test_unhealthy_bluetooth_report_rolls_back_with_reason(self):
        installed, plan, plan_path = self.plan()
        def launch(*args, **kwargs):
            if 'SWEETMETER_UPDATE_HEALTH' in kwargs['env']:
                Path(kwargs['env']['SWEETMETER_UPDATE_HEALTH']).with_name('unhealthy.json').write_text(json.dumps(
                    dict(version=plan['version'], nonce=plan['nonce'], pid=555,
                         reason='Bluetooth permission is not available')))
            return Process()
        stopped = []
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update, 'terminate_pid', side_effect=lambda pid, timeout=10: stopped.append(pid)), \
                patch.object(update.subprocess, 'Popen', side_effect=launch) as popen:
            with self.assertRaisesRegex(RuntimeError, 'Bluetooth permission'):
                update.apply_update(plan_path)
        self.assertIn(555, stopped)
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')
        self.assertEqual(popen.call_count, 2)
        result = json.loads((self.state / 'companion-update-result.json').read_text())
        self.assertIn('Bluetooth permission', result['reason'])

    def test_forged_health_receipt_is_ignored(self):
        installed, plan, plan_path = self.plan()
        def launch(*args, **kwargs):
            if 'SWEETMETER_UPDATE_HEALTH' in kwargs['env']:
                Path(kwargs['env']['SWEETMETER_UPDATE_HEALTH']).write_text(json.dumps(
                    dict(version=plan['version'], nonce='f' * 48, pid=444)))
            return Process(exit_after=3)
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', side_effect=lambda pid: pid == 444), \
                patch.object(update, 'terminate_pid'), \
                patch.object(update.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                update.apply_update(plan_path)
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')

    def test_confirmed_update_refreshes_launcher_from_new_bundle(self):
        installed, plan, plan_path = self.plan()
        candidate = Path(plan['candidate'])
        helper_folder = candidate / ('Contents/Frameworks' if sys.platform == 'darwin' else '_internal')
        helper_folder.mkdir(parents=True, exist_ok=True)
        (helper_folder / update.HELPER_NAME).write_bytes(b'new launcher')
        launcher = self.root / 'launcher' / update.LAUNCHER_NAME
        launcher.parent.mkdir()
        launcher.write_bytes(b'old launcher')
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', side_effect=lambda pid: pid == 444), \
                patch.object(update.subprocess, 'Popen', side_effect=self.launch_healthy(plan)):
            update.apply_update(plan_path)
        self.assertEqual(launcher.read_bytes(), b'new launcher')
        self.assertFalse(list(launcher.parent.glob('*.new')))

    def test_recovery_at_each_durable_swap_boundary(self):
        import shutil
        for stage in ('prepared', 'old_moved', 'new_installed', 'confirmed'):
            with self.subTest(stage=stage):
                folder = self.root / stage
                folder.mkdir()
                state = folder / 'state'
                state.mkdir()
                installed = folder / self.name
                incoming = folder / (self.name + '.incoming-abc123')
                backup = folder / (self.name + '.previous-abc123')
                old = installed if stage == 'prepared' else backup
                old.mkdir()
                (old / 'marker').write_text('old')
                new = incoming if stage in ('prepared', 'old_moved') else installed
                new.mkdir()
                (new / 'marker').write_text('new')
                journal = folder / 'companion-swap.json'
                journal.write_text(json.dumps(dict(schema=1, root=str(installed), nonce='abc123',
                    state_dir=str(state), version='2026.9.2', stage=stage)))
                with patch.object(update, 'install_root', return_value=installed), \
                        patch.object(update, 'data_dir', return_value=folder):
                    self.assertTrue(update.recover_update())
                    self.assertFalse(update.recover_update())
                self.assertEqual((installed / 'marker').read_text(), 'new' if stage == 'confirmed' else 'old')
                self.assertFalse(incoming.exists())
                self.assertFalse(backup.exists())
                self.assertFalse(journal.exists())
                outcome = json.loads((state / 'companion-update-result.json').read_text())
                self.assertEqual(outcome['status'], 'success' if stage == 'confirmed' else 'rollback')
                shutil.rmtree(folder)

    def test_recovery_refuses_running_app_or_active_update(self):
        from meter.instance_lock import InstanceLock
        installed, plan, _ = self.plan()
        journal = self.root / 'companion-swap.json'
        journal.write_text(json.dumps(dict(plan, stage='prepared')))
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root):
            instance = InstanceLock(self.state / 'meter.lock')
            try:
                with self.assertRaisesRegex(RuntimeError, 'Quit the running'):
                    update.recover_update()
            finally:
                instance.close()
            with update._update_lock():
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    update.recover_update()
        self.assertTrue(journal.exists())
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')

    def installed_app(self):
        installed = self.root / self.name
        (installed / self.relative_exe).parent.mkdir(parents=True)
        (installed / self.relative_exe).write_text('app')
        return installed

    def test_startup_launcher_recovers_then_starts_app(self):
        from unittest.mock import Mock
        installed = self.installed_app()
        process = Mock()
        process.wait.return_value = 0
        with patch.object(update, 'recover_update') as recover, \
                patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update.subprocess, 'Popen', return_value=process) as launch:
            self.assertEqual(update.launch_installed(['--background']), 0)
        recover.assert_called_once()
        command = launch.call_args.args[0]
        self.assertEqual(command[-1], '--background')
        if sys.platform == 'darwin':
            # LaunchServices, in the background: TCC attributes Bluetooth to the bundle.
            self.assertEqual(command[:3], ['/usr/bin/open', '-n', '-g'])
            self.assertIn(str(installed), command)
            process.wait.assert_called_once_with()
        elif sys.platform == 'win32':
            process.wait.assert_not_called()
            self.assertTrue(launch.call_args.kwargs['creationflags'] & update.CREATE_NO_WINDOW)
        else:
            # The XDG autostart unit must stay alive while the app runs.
            self.assertEqual(command[0], str(installed / self.relative_exe))
            process.wait.assert_called_once_with()

    def test_launcher_does_not_start_a_second_copy_during_recovery_conflict(self):
        with patch.object(update, 'recover_update', side_effect=RuntimeError('Quit the running Sweetmeter')), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update.subprocess, 'Popen') as launch:
            self.assertEqual(update.launch_installed(['--background']), 0)
        launch.assert_not_called()
        self.assertIn('Quit the running', (self.root / 'helper.log').read_text())

    def test_helper_gives_the_app_a_deadline_beyond_the_old_150_seconds(self):
        import time
        installed, plan, plan_path = self.plan()
        plan['health_timeout'] = update.HEALTH_TIMEOUT
        plan_path.write_text(json.dumps(plan))
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', side_effect=lambda pid: pid == 444), \
                patch.object(update.subprocess, 'Popen', side_effect=self.launch_healthy(plan)) as popen:
            update.apply_update(plan_path)
        deadline = float(popen.call_args_list[0].kwargs['env']['SWEETMETER_UPDATE_DEADLINE'])
        self.assertAlmostEqual(deadline - time.time(), update.HEALTH_TIMEOUT, delta=10)

    def test_hung_app_that_never_reported_its_pid_is_stopped_and_its_tree_kept(self):
        installed, plan, plan_path = self.plan()
        running = {777}
        stopped = []
        self.tree_pids.side_effect = lambda root: set(running) if Path(root) == installed else set()
        def terminate(pid, timeout=10):
            stopped.append(pid)
            return False  # Hangs: cannot be stopped.
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update, 'terminate_pid', side_effect=terminate), \
                patch.object(update.subprocess, 'Popen', return_value=Process(exit_after=2)):
            with self.assertRaises(RuntimeError):
                update.apply_update(plan_path)
        self.assertIn(777, stopped)  # Found by its executable path, not a receipt.
        # Never delete the tree the hung process runs from; the launcher
        # finishes the rollback once it has exited.
        self.assertEqual((installed / self.relative_exe).read_text(), 'new app')
        self.assertTrue((self.root / 'companion-swap.json').exists())
        result = json.loads((self.state / 'companion-update-result.json').read_text())
        self.assertIn('next login', result['reason'])
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root):
            with self.assertRaisesRegex(RuntimeError, 'Quit the running'):
                update.recover_update()
            running.clear()  # It exited: the launcher now restores the previous version.
            self.assertTrue(update.recover_update())
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')

    def test_launcher_does_not_start_a_second_copy_of_a_running_app(self):
        from meter.instance_lock import InstanceLock
        installed = self.installed_app()
        state = self.root / 'state-running'
        state.mkdir()
        lock = InstanceLock(state / 'meter.lock')
        self.addCleanup(lock.close)
        with patch.object(update, 'recover_update'), \
                patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update.subprocess, 'Popen') as launch:
            self.assertEqual(update.launch_installed(['--background', '--state-dir', str(state)]), 0)
            launch.assert_not_called()
            lock.close()
            update.launch_installed(['--background', '--state-dir', str(state)])
            launch.assert_called_once()

    def test_launcher_applies_saved_profile_environment(self):
        from unittest.mock import Mock
        installed = self.installed_app()
        (self.root / 'startup-environment.json').write_text(json.dumps({'CODEX_HOME': '/x y/codex', 'EVIL': '1'}))
        with patch.object(update, 'recover_update'), \
                patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.dict(os.environ), \
                patch.object(update.subprocess, 'Popen', return_value=Mock()) as launch:
            os.environ.pop('CODEX_HOME', None)
            update.launch_installed([])
        environment = launch.call_args.kwargs['env']
        self.assertEqual(environment['CODEX_HOME'], '/x y/codex')
        self.assertNotIn('EVIL', environment)
        if sys.platform == 'darwin':
            self.assertIn('CODEX_HOME=/x y/codex', launch.call_args.args[0])

    def test_cleanup_keeps_staging_that_an_update_still_needs(self):
        updates = self.state / 'updates'
        old, current, manual = updates / 'companion-old', updates / 'companion-current', updates / 'companion-manual'
        for folder in (old, current, manual):
            folder.mkdir(parents=True)
        with patch.object(update, 'data_dir', return_value=self.root), \
                patch.dict(os.environ, SWEETMETER_UPDATE_HEALTH=str(current / 'healthy.json')):
            (self.root / 'companion-swap.json').write_text('{}')
            self.assertEqual(update.cleanup_staging(self.state), [])
            (self.root / 'companion-swap.json').unlink()
            with update._update_lock():
                self.assertEqual(update.cleanup_staging(self.state), [])  # A helper is running.
            self.assertEqual(update.cleanup_staging(self.state, keep=[manual]), [old])
        self.assertTrue(current.exists() and manual.exists())
        self.assertFalse(old.exists())

    def test_windows_arm64_accepts_x64_package(self):
        with patch.object(update, 'compatible_platforms', return_value=[('windows', 'arm64'), ('windows', 'x86_64')]):
            self.assertTrue(update.package_matches_host({'os': 'windows', 'arch': 'x86_64'}))
            self.assertFalse(update.package_matches_host({'os': 'linux', 'arch': 'x86_64'}))


class Process:
    """Stand-in for the started app (or `open -W` on macOS)."""
    def __init__(self, pid=444, exit_code=None, exit_after=None):
        self.pid, self.exit_code, self.exit_after = pid, exit_code, exit_after
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.terminated or (self.exit_after is not None and self.polls > self.exit_after):
            return self.exit_code if self.exit_code is not None else 1
        return self.exit_code

    def terminate(self):
        self.terminated = True

    kill = terminate

    def wait(self, timeout=None):
        return 0


class HealthReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.marker = self.folder / 'healthy.json'

    def environment(self, baseline=None):
        values = {'SWEETMETER_UPDATE_HEALTH': str(self.marker), 'SWEETMETER_UPDATE_NONCE': 'n' * 12}
        if baseline is not None:
            values['SWEETMETER_UPDATE_BLUETOOTH'] = baseline
        return patch.dict(os.environ, values)

    def test_without_update_only_confirms_locally(self):
        confirmed = []
        with patch.dict(os.environ):
            for key in update.UPDATE_ENVIRONMENT:
                os.environ.pop(key, None)
            self.assertIsNone(update.confirm_update_health('2026.9.2', on_confirmed=lambda: confirmed.append(1)))
        self.assertEqual(confirmed, [1])
        self.assertFalse(self.marker.exists())

    def test_old_helper_without_bluetooth_baseline_gets_immediate_receipt(self):
        with self.environment():
            os.environ.pop('SWEETMETER_UPDATE_BLUETOOTH', None)
            update.confirm_update_health('2026.9.2', radio=SimpleNamespace(health=None))
        receipt = json.loads(self.marker.read_text())
        self.assertEqual((receipt['version'], receipt['nonce'], receipt['pid']), ('2026.9.2', 'n' * 12, os.getpid()))

    def test_bluetooth_ok_or_off_confirms_without_blocking(self):
        for health in ('ok', 'off'):
            self.marker.unlink(missing_ok=True)
            radio = SimpleNamespace(health=None)
            confirmed = threading.Event()
            with self.environment('ok'):
                thread = update.confirm_update_health('2026.9.2', radio=radio, on_confirmed=confirmed.set,
                                                      interval=.01)
            self.assertIsNotNone(thread)  # Returned immediately; the Tk mainloop keeps running.
            self.assertFalse(self.marker.exists())
            radio.health = health
            self.assertTrue(confirmed.wait(5))
            thread.join(5)
            self.assertTrue(self.marker.exists())
            self.assertTrue((self.folder / 'started.json').exists())

    def test_persisting_unauthorized_is_reported_and_never_confirmed(self):
        radio = SimpleNamespace(health='unauthorized')
        notices = []
        with self.environment('ok'):
            thread = update.confirm_update_health('2026.9.2', radio=radio, notify=notices.append,
                                                  timeout=.2, interval=.01)
        thread.join(5)
        self.assertFalse(self.marker.exists())
        failure = json.loads((self.folder / 'unhealthy.json').read_text())
        self.assertEqual(failure['bluetooth'], 'unauthorized')
        self.assertIn('Privacy & Security', failure['reason'])
        self.assertEqual(len(notices), 1)

    def test_previously_broken_bluetooth_is_not_required(self):
        # No update loop when the previous version already lacked permission.
        with self.environment('unauthorized'):
            self.assertIsNone(update.confirm_update_health('2026.9.2', radio=SimpleNamespace(health='unauthorized')))
        self.assertTrue(self.marker.exists())

    def test_open_permission_prompt_is_waited_for_and_never_rolls_back(self):
        # macOS shows its Bluetooth prompt: the radio reports nothing (None).
        radio = SimpleNamespace(health=None)
        notices, confirmed = [], threading.Event()
        with self.environment('ok'), patch.object(update, 'BLUETOOTH_PROMPT_NOTICE_AFTER', .05):
            thread = update.confirm_update_health('2026.9.2', radio=radio, notify=notices.append,
                                                  on_confirmed=confirmed.set, timeout=.4, interval=.01)
            thread.join(5)
        self.assertTrue(confirmed.is_set())  # Unknown at the deadline: the update is kept.
        self.assertTrue(self.marker.exists())
        self.assertFalse((self.folder / 'unhealthy.json').exists())
        self.assertEqual(len(notices), 1)
        self.assertIn('Allow Bluetooth for Sweetmeter', notices[0])
        self.assertNotIn('None', notices[0])

    def test_restarted_radio_is_followed(self):
        app = SimpleNamespace(radio=SimpleNamespace(health=None))
        confirmed = threading.Event()
        with self.environment('ok'):
            thread = update.confirm_update_health('2026.9.2', radio=lambda: app.radio, on_confirmed=confirmed.set,
                                                  timeout=5, interval=.01)
        app.radio = None  # Bluetooth worker stopped; the app restarts it...
        app.radio = SimpleNamespace(health='ok')  # ...and the new worker works.
        self.assertTrue(confirmed.wait(5))
        thread.join(5)
        self.assertTrue(self.marker.exists())

    def test_radio_that_failed_to_start_rolls_back_with_a_plain_reason(self):
        with self.environment('ok'):
            thread = update.confirm_update_health('2026.9.2', radio=lambda: None, radio_failed=lambda: True,
                                                  timeout=5, interval=.01)
        thread.join(5)
        self.assertFalse(self.marker.exists())
        failure = json.loads((self.folder / 'unhealthy.json').read_text())
        self.assertEqual(failure['bluetooth'], 'failed')
        self.assertIn('Bluetooth did not start', failure['reason'])
        self.assertNotIn('None', failure['reason'])

    def test_helper_deadline_sets_the_wait(self):
        import time
        with patch.dict(os.environ, SWEETMETER_UPDATE_DEADLINE='%.3f' % (time.time() + 180)):
            self.assertAlmostEqual(update._health_timeout(110), 180 - update.BLUETOOTH_DEADLINE_MARGIN, delta=2)
        with patch.dict(os.environ, SWEETMETER_UPDATE_DEADLINE='garbage'):
            self.assertEqual(update._health_timeout(110), 110)
        os.environ.pop('SWEETMETER_UPDATE_DEADLINE', None)
        self.assertEqual(update._health_timeout(110), 110)  # Older helper: stay inside its 150 s.
        self.assertLess(update.BLUETOOTH_HEALTH_TIMEOUT + 30, 150)
        self.assertGreaterEqual(update.HEALTH_TIMEOUT, 165)


class TreePidsTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'Windows refuses to move a running executable instead')
    def test_finds_a_process_running_from_the_tree(self):
        import shutil
        import subprocess
        import time
        source = shutil.which('sleep')
        if source is None:
            self.skipTest('no sleep executable')
        with tempfile.TemporaryDirectory() as folder:
            tree = Path(folder) / 'Sweetmeter.app'
            executable = tree / 'Contents/MacOS/Sweetmeter'
            executable.parent.mkdir(parents=True)
            if sys.platform == 'darwin':
                # macOS kills a copied system binary (code signature); ps
                # reports the path a program was started by, so link it.
                executable.symlink_to(source)
            else:
                shutil.copyfile(source, executable)  # /proc/<pid>/exe resolves links.
                executable.chmod(0o755)
            process = subprocess.Popen([str(executable), '30'])
            try:
                found = set()
                for _ in range(50):
                    found = update.tree_pids(tree)
                    if process.pid in found:
                        break
                    time.sleep(.05)
                self.assertIn(process.pid, found)
                self.assertNotIn(process.pid, update.tree_pids(Path(folder) / 'Other.app'))
            finally:
                process.kill()
                process.wait(5)
            self.assertNotIn(process.pid, update.tree_pids(tree))


if __name__ == '__main__':
    unittest.main()
