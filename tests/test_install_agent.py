"""Source installs install dependencies once per requirements + interpreter."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_agent


class RequirementInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = self.root / 'requirements.txt'
        self.requirements.write_text('bleak==3.0.2\n')
        self.marker = self.root / 'marker'
        self.identity = 'venv/python\n3.12.5\ncpython-312'

    def install(self, identity=None):
        with patch.object(install_agent.subprocess, 'run') as run:
            installed = install_agent.install_requirements(self.root / 'python', self.requirements, self.marker,
                                                           identity or self.identity)
        return installed, run

    def test_unchanged_requirements_skip_pip(self):
        self.assertTrue(self.install()[0])
        installed, run = self.install()
        self.assertFalse(installed)
        run.assert_not_called()

    def test_changed_requirements_install_again(self):
        self.install()
        self.requirements.write_text('bleak==3.0.3\n')
        installed, run = self.install()
        self.assertTrue(installed)
        run.assert_called_once()

    def test_changed_interpreter_installs_again(self):
        self.install()
        installed, run = self.install('venv/python\n3.13.1\ncpython-313')
        self.assertTrue(installed)
        run.assert_called_once()
        with patch.object(install_agent.sys, 'version', '9.9.9 (other installer)'):
            installed, run = self.install('venv/python\n3.13.1\ncpython-313')
        self.assertTrue(installed)

    def test_failed_pip_does_not_mark_success(self):
        self.install()
        self.requirements.write_text('bleak==3.0.4\n')
        with patch.object(install_agent.subprocess, 'run', side_effect=RuntimeError('pip failed')):
            with self.assertRaises(RuntimeError):
                install_agent.install_requirements(self.root / 'python', self.requirements, self.marker,
                                                   self.identity)
        self.assertFalse(self.marker.exists())

    def test_interpreter_identity_reports_real_python(self):
        identity = install_agent.interpreter_identity(Path(sys.executable))
        self.assertIn(sys.version.split()[0], identity)
        self.assertIsNone(install_agent.interpreter_identity(self.root / 'missing-python'))


if __name__ == '__main__':
    unittest.main()
