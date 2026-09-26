"""Entry point: logging, self-test backend, plain setup errors, helper modes."""
import isolation  # noqa: F401  (test sandbox; must be the first import)
import importlib
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from meter import __main__ as entry


class LoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.addCleanup(self.reset)

    @staticmethod
    def reset():
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def test_background_process_logs_once_to_rotating_file(self):
        with patch.object(sys, 'stderr', io.StringIO()):
            handlers = entry.configure_logging(self.state)
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], RotatingFileHandler)
        self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1024 * 1024, 3))
        logging.info('only once')
        handlers[0].flush()
        self.assertEqual((self.state / 'agent.log').read_text().count('only once'), 1)

    def test_interactive_terminal_also_gets_console_output(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with patch.object(sys, 'stderr', Terminal()):
            handlers = entry.configure_logging(self.state)
        self.assertEqual(len(handlers), 2)

    def test_windowless_app_without_stderr(self):
        with patch.object(sys, 'stderr', None):
            self.assertEqual(len(entry.configure_logging(self.state)), 1)

    def test_log_rotates(self):
        with patch.object(sys, 'stderr', None), patch.object(entry, 'LOG_BYTES', 200):
            handlers = entry.configure_logging(self.state)
        for number in range(40):
            logging.info('line %d with padding to force rotation', number)
        handlers[0].flush()
        self.assertTrue((self.state / 'agent.log.1').exists())
        self.assertFalse((self.state / 'agent.log.4').exists())


class SelfTestTests(unittest.TestCase):
    def test_platform_backend_is_imported(self):
        self.assertIn('corebluetooth', entry.bluetooth_backend_modules('darwin')[0])
        self.assertIn('winrt', entry.bluetooth_backend_modules('win32')[0])
        self.assertIn('bluezdbus', entry.bluetooth_backend_modules('linux')[0])
        with patch.object(entry.importlib if hasattr(entry, 'importlib') else importlib, 'import_module',
                          side_effect=ImportError('missing backend')):
            with self.assertRaises(ImportError):
                entry.self_test()


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        environment = patch.dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / 'data'),
                                 LOCALAPPDATA=str(home / 'local'))
        environment.start()
        self.addCleanup(environment.stop)
        self.umask = os.umask(0o022)
        self.addCleanup(os.umask, self.umask)

    def test_install_failure_is_one_plain_line(self):
        from meter.paths import data_dir
        error = io.StringIO()
        with patch('meter.installation.install_current', side_effect=ValueError('Choose the extracted folder.')), \
                patch.object(sys, 'stderr', error):
            self.assertEqual(entry.main(['--install']), 2)
        self.assertEqual(error.getvalue(), 'Sweetmeter setup did not finish: Choose the extracted folder.\n')
        self.assertIn('Choose the extracted folder.', (data_dir() / 'install-error.txt').read_text())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permission mask')
    def test_install_sets_private_umask_first(self):
        seen = []
        def install(**kwargs):
            current = os.umask(0)
            os.umask(current)
            seen.append(current)
        with patch('meter.installation.install_current', side_effect=install):
            self.assertEqual(entry.main(['--install']), 0)
        self.assertEqual(seen, [0o077])

    def test_copy_that_fails_its_self_test_never_installs_itself(self):
        error = io.StringIO()
        with patch.object(entry, 'self_test', side_effect=ImportError('bleak backend missing')), \
                patch('meter.installation.install_current') as install, patch.object(sys, 'stderr', error):
            self.assertEqual(entry.main(['--install']), 2)
        install.assert_not_called()
        self.assertIn('failed its self-test', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_install_outcome_is_reported_for_the_installer(self):
        from meter.paths import data_dir
        def install(start_at_login, report):
            self.assertIsNone(start_at_login)  # Keeps a recorded "Start at login" choice.
            report('Updated Sweetmeter 2026.9.1 to 2026.9.2.')
        output = io.StringIO()
        with patch.object(entry, 'self_test'), patch('meter.installation.install_current', side_effect=install), \
                patch.object(sys, 'stdout', output):
            self.assertEqual(entry.main(['--install']), 0)
        self.assertEqual(output.getvalue(), 'Updated Sweetmeter 2026.9.1 to 2026.9.2.\n')
        self.assertEqual((data_dir() / 'install-result.txt').read_text(), 'Updated Sweetmeter 2026.9.1 to 2026.9.2.\n')

    def test_uninstall_flags_are_forwarded(self):
        with patch('meter.installation.uninstall_main', return_value=0) as uninstall:
            self.assertEqual(entry.main(['--uninstall', '--remove-data']), 0)
        uninstall.assert_called_once_with(['--remove-data'])

    def test_background_duplicate_does_not_pop_the_window(self):
        from meter.instance_lock import InstanceLock
        state = Path(os.environ['HOME']) / 'state'
        state.mkdir()
        lock = InstanceLock(state / 'meter.lock')
        self.addCleanup(lock.close)
        self.assertEqual(entry.main(['--background', '--state-dir', str(state)]), 0)
        self.assertFalse((state / 'show-window').exists())
        with patch('sys.stdout', io.StringIO()):
            self.assertEqual(entry.main(['--state-dir', str(state)]), 0)
        self.assertTrue((state / 'show-window').exists())


class HelperTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        self.addCleanup(sys.path.remove, str(ROOT / 'scripts'))
        self.helper = importlib.import_module('update_helper')

    def test_failures_are_logged_not_raised(self):
        with patch('meter.self_update.apply_update', side_effect=RuntimeError('boom')), \
                patch('meter.self_update.helper_log') as log:
            self.assertEqual(self.helper.main(['/plan.json']), 1)
        self.assertIn('boom', log.call_args.args[0])

    def test_modes(self):
        with patch('meter.self_update.launch_installed', return_value=0) as launch, \
                patch.object(self.helper, '_cap_launchd_output') as cap:
            self.assertEqual(self.helper.main(['--launch', '--background']), 0)
        launch.assert_called_once_with(['--background'])
        cap.assert_called_once()
        with patch('meter.installation.uninstall_main', return_value=0) as uninstall:
            self.assertEqual(self.helper.main(['--uninstall', '--remove-data']), 0)
        uninstall.assert_called_once_with(['--remove-data'])
        with patch('meter.self_update.helper_log'):
            self.assertEqual(self.helper.main([]), 2)

    def test_launcher_output_cap_only_touches_the_sandboxed_state(self):
        import isolation
        from meter.paths import default_state_dir
        state = default_state_dir()
        self.assertTrue(os.path.realpath(state).startswith(os.path.realpath(isolation.SANDBOX)))
        state.mkdir(parents=True, exist_ok=True)
        log = state / 'launcher-output.log'
        log.write_bytes(b'x' * (1024 * 1024 + 1))
        self.helper._cap_launchd_output()
        self.assertEqual(log.stat().st_size, 0)
        log.write_bytes(b'small')
        self.helper._cap_launchd_output()
        self.assertEqual(log.read_bytes(), b'small')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permission mask')
    def test_helper_launcher_and_uninstaller_use_a_private_umask(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        seen = []

        def record(*args, **kwargs):
            current = os.umask(0)
            os.umask(current)
            seen.append(current)
            return 0
        for argv, target in ((['--launch'], 'meter.self_update.launch_installed'),
                             (['--uninstall'], 'meter.installation.uninstall_main'),
                             (['/plan.json'], 'meter.self_update.apply_update')):
            os.umask(0o022)
            with patch(target, side_effect=record), patch.object(self.helper, '_cap_launchd_output'):
                self.helper.main(argv)
        self.assertEqual(seen, [0o077] * 3)


if __name__ == '__main__':
    unittest.main()
