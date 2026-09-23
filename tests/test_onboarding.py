import isolation  # noqa: F401  (test sandbox; must be the first import)
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from meter import installation, onboarding
from meter.gui import Desktop


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.name = 'Sweetmeter.app' if sys.platform == 'darwin' else 'Sweetmeter'
        self.source = self.root / 'download' / self.name
        self.destination = self.root / 'managed' / self.name
        self.exe = Path(installation.app_command(self.source)[0])
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b'fixture executable')
        version = self.source / ('Contents/Resources/VERSION' if sys.platform == 'darwin' else '_internal/VERSION')
        version.parent.mkdir(parents=True, exist_ok=True)
        version.write_text('2026.9.1\n')
        self.data = self.root / 'data'
        self.state = self.data / 'state'
        # Any path function not patched below must still resolve inside the
        # sandbox, never to the real user's app, LaunchAgent or registry.
        home = self.root / 'home'
        environment = patch.dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                                 XDG_DATA_HOME=str(home / 'data'), XDG_CONFIG_HOME=str(home / 'config'),
                                 LOCALAPPDATA=str(home / 'local'), APPDATA=str(home / 'roaming'))
        environment.start()
        self.addCleanup(environment.stop)
        for name, result in (('install_root', self.destination), ('data_dir', self.data),
                             ('default_state_dir', self.state), ('default_install_root', self.destination),
                             ('candidate_install_roots', [self.destination])):
            p = patch.object(installation, name, return_value=result)
            p.start()
            self.addCleanup(p.stop)
        for target in ('meter.installation.startup', 'meter.installation.native_startup_command',
                       'meter.installation.retire_legacy_startup'):
            guard = patch(target, side_effect=AssertionError('unexpected real startup change in a test'))
            guard.start()
            self.addCleanup(guard.stop)
        # Never write the real Windows registry from tests.
        registry = patch.object(installation, 'register_uninstall_entry')
        registry.start()
        self.addCleanup(registry.stop)

    def test_install_opens_window_and_rerun_preserves_app_and_identity(self):
        with patch.object(installation, 'startup') as startup, \
                patch.object(installation, 'retire_legacy_startup'), \
                patch.object(installation, 'native_startup_command', return_value=['helper']), \
                patch.object(installation.subprocess, 'Popen') as launch, \
                patch.object(installation.subprocess, 'run', return_value=Mock(returncode=0)) as self_test:
            installation.install_native(self.source)
            self.assertTrue((self.state / 'show-window').exists())
            startup.assert_called_once()
            self.assertTrue(startup.call_args.kwargs['start_now'])
            (self.state / 'companion.json').write_text('keep identity')
            (self.state / 'show-window').unlink()
            self.exe.write_bytes(b'different download')
            # A rerun of the same version finds the installed copy healthy
            # (its own self-test passes): it only repairs registration.
            installation.install_native(self.source)
            self.assertEqual(self_test.call_args.args[0][-1], '--self-test')
            self.assertEqual(startup.call_count, 2)
            self.assertTrue(startup.call_args.kwargs['start_now'])
            self.assertTrue((self.state / 'show-window').exists())
            self.assertEqual(Path(installation.app_command(self.destination)[0]).read_bytes(), b'fixture executable')
            self.assertEqual((self.state / 'companion.json').read_text(), 'keep identity')
            launch.assert_not_called()

    def test_install_without_login_startup_opens_the_app(self):
        with patch.object(installation, 'startup') as startup, \
                patch.object(installation, 'native_startup_command', return_value=['helper']), \
                patch.object(installation.subprocess, 'Popen') as launch, \
                patch('meter.self_update.subprocess.Popen') as app_launch:
            installation.install_native(self.source, start_at_login=False)
        startup.assert_not_called()  # Login startup off and none registered: nothing to create or remove.
        launch.assert_not_called()
        app_launch.assert_called_once()
        self.assertEqual(app_launch.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        command = app_launch.call_args.args[0]
        if sys.platform == 'darwin':
            # LaunchServices makes the bundle its own privacy-responsible process.
            self.assertEqual(command[:2], ['/usr/bin/open', '-n'])
            self.assertIn(str(self.destination), command)
        else:
            self.assertEqual(command, installation.app_command(self.destination))

    def test_failed_copy_does_not_block_retry_with_partial_install(self):
        with patch.object(installation.shutil, 'copytree', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): installation.install_native(self.source)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.data / 'install.json').exists())

    def test_unmanaged_existing_folder_is_never_overwritten_or_executed(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'keep').write_text('keep')
        with patch.object(installation.subprocess, 'Popen') as launch:
            with self.assertRaises((OSError, ValueError)):
                installation.install_native(self.source)
            launch.assert_not_called()
        self.assertEqual((self.destination / 'keep').read_text(), 'keep')

    def test_welcome_only_for_interactive_downloaded_native_app(self):
        args = SimpleNamespace(background=False, headless=False, once=False,
                               preview_only=False, state_dir=None)
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(self.exe)), \
                patch.object(onboarding, 'install_root', return_value=self.destination):
            self.assertTrue(onboarding.needs_install(args))
            for flag in ('background', 'headless', 'once', 'preview_only', 'state_dir'):
                setattr(args, flag, True)
                self.assertFalse(onboarding.needs_install(args))
                setattr(args, flag, False)
            with patch.object(sys, 'executable', installation.app_command(self.destination)[0]):
                self.assertFalse(onboarding.needs_install(args))
        self.assertFalse(onboarding.needs_install(args))

    def test_setup_is_ready_only_after_display_ack(self):
        desktop = Desktop.__new__(Desktop)
        for name in ('connection', 'setup_heading', 'setup_help'):
            setattr(desktop, name, Mock())
        desktop.event({'event': 'registered', 'name': 'My Mac'})
        self.assertIn('My Mac', desktop.setup_help.set.call_args.args[0])
        desktop.event({'event': 'connected'})
        self.assertNotEqual(desktop.setup_heading.set.call_args.args[0], 'Ready')
        desktop.event({'event': 'ack'})
        self.assertEqual(desktop.setup_heading.set.call_args.args[0], 'Ready')
        desktop.event({'event': 'disconnected'})
        self.assertNotEqual(desktop.setup_heading.set.call_args.args[0], 'Ready')
