import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
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
        self.package = self.root / 'package.zip'
        self.name = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
        self.relative_exe = ('Contents/MacOS/Sweetmeter' if sys.platform == 'darwin'
                             else 'Sweetmeter.exe' if sys.platform == 'win32' else 'Sweetmeter')

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self):
        return dict(kind='companion', version='2026.9.2',
                    os={'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux'),
                    arch={'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower()),
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
                    state_dir=str(self.state), version='2026.9.2', nonce='abc123', health_timeout=10)
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
        class Process:
            pid = 444
            def poll(self): return None
        def launch(*args, **kwargs):
            Path(kwargs['env']['SWEETMETER_UPDATE_HEALTH']).write_text(json.dumps(
                dict(version=plan['version'], nonce=plan['nonce'], pid=444)))
            return Process()
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update.os, 'fsync', side_effect=windows_fsync), \
                patch.object(update.subprocess, 'Popen', side_effect=launch):
            update.apply_update(plan_path)
        self.assertEqual((installed / self.relative_exe).read_text(), 'new app')
        self.assertEqual(json.loads((self.state / 'companion-update-result.json').read_text())['status'], 'success')
        self.assertFalse(list(self.root.glob('*.previous-*')))

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
        class Process:
            pid = 444
            def poll(self): return 1
        with patch.object(update, 'install_root', return_value=installed), \
                patch.object(update, 'data_dir', return_value=self.root), \
                patch.object(update, '_alive', return_value=False), \
                patch.object(update.subprocess, 'Popen', return_value=Process()) as launch:
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                update.apply_update(plan_path)
        self.assertEqual((installed / self.relative_exe).read_text(), 'old app')
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(json.loads((self.state / 'companion-update-result.json').read_text())['status'], 'rollback')

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

    def test_startup_launcher_waits_for_child_lifetime(self):
        from unittest.mock import Mock
        process = Mock()
        process.wait.return_value = 0
        with patch.object(update, 'recover_update') as recover, \
                patch.object(update.subprocess, 'Popen', return_value=process) as launch:
            self.assertEqual(update.launch_installed(['--background']), 0)
        recover.assert_called_once()
        process.wait.assert_called_once_with()
        self.assertEqual(launch.call_args.args[0][-1], '--background')


if __name__ == '__main__':
    unittest.main()
