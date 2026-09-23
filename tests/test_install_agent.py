"""Source installs reuse an existing virtual environment's dependencies."""
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

    def install(self):
        with patch.object(install_agent.subprocess, 'run') as run:
            installed = install_agent.install_requirements(self.root / 'python', self.requirements, self.marker)
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

    def test_failed_pip_does_not_mark_success(self):
        with patch.object(install_agent.subprocess, 'run', side_effect=RuntimeError('pip failed')):
            with self.assertRaises(RuntimeError):
                install_agent.install_requirements(self.root / 'python', self.requirements, self.marker)
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
