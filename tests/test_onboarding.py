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
        self.data = self.root / 'data'
        self.state = self.data / 'state'
        for name, result in (('install_root', self.destination), ('data_dir', self.data),
                             ('default_state_dir', self.state)):
            p = patch.object(installation, name, return_value=result)
            p.start()
            self.addCleanup(p.stop)

    def test_install_opens_window_and_rerun_preserves_app_and_identity(self):
        with patch.object(installation, 'startup') as startup, \
                patch.object(installation, 'retire_legacy_startup'), \
                patch.object(installation, 'native_startup_command', return_value=['helper']), \
                patch.object(installation.subprocess, 'Popen') as launch:
            installation.install_native(self.source)
            self.assertTrue((self.state / 'show-window').exists())
            startup.assert_called_once()
            (self.state / 'companion.json').write_text('keep identity')
            self.exe.write_bytes(b'different download')
            installation.install_native(self.source)
            self.assertEqual(Path(installation.app_command(self.destination)[0]).read_bytes(), b'fixture executable')
            self.assertEqual((self.state / 'companion.json').read_text(), 'keep identity')
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')

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
