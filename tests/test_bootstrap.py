"""Run the real installers against signed local fixtures, never user accounts."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = (ROOT / 'meter/assets/keys/release-1.pem').read_text().strip()
PUBLIC_DER = ''.join(PUBLIC.splitlines()[1:-1])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.mark = self.root / 'launched'
        self.system = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
        self.linux_arch = 'x86_64'
        self.bluez_installed = True
        self.package_log = self.root / 'package-manager'
        self.arch = 'arm64' if sys.platform == 'darwin' and platform.machine() == 'arm64' else 'x86_64'
        self.version = '2026.9.999'
        self.asset = f'Sweetmeter-{self.version}-{self.system}-{self.arch}.zip'
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode().strip()
        self.environment = dict(os.environ, HOME=str(self.home), LOCALAPPDATA=str(self.home),
                                XDG_DATA_HOME=str(self.home / 'data'), DISPLAY=':99',
                                PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        entry = {'macos': 'Sweetmeter.app/Contents/MacOS/Sweetmeter',
                 'windows': 'Sweetmeter/Sweetmeter.exe', 'linux': 'Sweetmeter/Sweetmeter'}[self.system]
        with zipfile.ZipFile(self.root / self.asset, 'w') as archive:
            item = zipfile.ZipInfo(entry)
            item.create_system = 3
            item.external_attr = 0o100755 << 16
            archive.writestr(item, '#!/bin/sh\nprintf "%s" "$1" > ' + self.quote(self.mark) + '\n')
        package = (self.root / self.asset).read_bytes()
        self.manifest = dict(schema=1, product='Sweetmeter', channel='stable', version=self.version,
                             artifacts=[dict(kind='companion', os=self.system, arch=self.arch,
                                             asset=self.asset, version=self.version, size=len(package),
                                             sha256=hashlib.sha256(package).hexdigest())])
        self.sign()

    @staticmethod
    def quote(value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    def sign(self):
        raw = json.dumps(self.manifest).encode()
        (self.root / 'manifest.json').write_bytes(raw)
        (self.root / 'manifest.json.sig').write_bytes(self.key.sign(raw, ec.ECDSA(hashes.SHA256())))

    def shell_tool(self, name, content):
        target = self.bin / name
        target.write_text('#!/bin/sh\n' + content + '\n')
        target.chmod(0o755)

    def run_installer(self):
        if sys.platform == 'win32':
            script = (ROOT / 'install.ps1').read_text().replace(PUBLIC_DER, ''.join(self.public.splitlines()[1:-1]))
            (self.root / 'install.ps1').write_text(script)
            # Network/launch boundaries mocked; Windows CNG and extraction are real.
            folder = str(self.root).replace("'", "''")
            wrapper = f"""
$ErrorActionPreference = 'Stop'
function Invoke-RestMethod {{ return @{{tag_name='2026.9.999'; draft=$false; prerelease=$false}} }}
function Invoke-WebRequest($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec) {{
  Copy-Item -LiteralPath (Join-Path '{folder}' ($Uri.Split('/')[-1])) -Destination $OutFile
}}
function Start-Process($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru) {{
  if ($Wait) {{ throw 'Do not wait for the background companion process tree.' }}
  Set-Content -LiteralPath '{folder}\\launched' -Value $ArgumentList
  $process = [PSCustomObject]@{{ExitCode=0}}
  $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{}}
  return $process
}}
& '{folder}\\install.ps1'
"""
            harness = self.root / 'harness.ps1'
            harness.write_text(wrapper)
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(harness)]
        else:
            script = (ROOT / 'install.sh').read_text().replace(PUBLIC, self.public)
            installer = self.root / 'install.sh'
            installer.write_text(script)
            # The Mac mini runs Linux ARM64 CI. Exercise the supported x64
            # target using a shell executable fixture, not a host-native binary.
            if self.system == 'linux':
                self.shell_tool('uname', 'case "$1" in -s) echo Linux ;; -m) echo ' + self.linux_arch + ' ;; esac')
            self.shell_tool('id', 'echo 501')
            self.shell_tool('apt-get', 'echo "apt-get $*" >> ' + self.quote(self.package_log))
            self.shell_tool('sudo', 'echo "sudo $*" >> ' + self.quote(self.package_log))
            if self.bluez_installed:
                self.shell_tool('bluetoothctl', 'exit 0')
            self.shell_tool('systemctl', 'exit 0')
            self.shell_tool('curl', f'''output=''
previous=''
url=''
for arg do
  if [ "$previous" = -o ]; then output=$arg; fi
  case "$arg" in https://*) url=$arg ;; esac
  previous=$arg
done
case "$url" in
  */releases/latest) printf 'https://github.com/luvxinc/Sweetmeter/releases/tag/2026.9.999' ;;
  */*) cp {self.quote(self.root)}/"${{url##*/}}" "$output" ;;
esac''')
            command = ['sh', str(installer)]
        return subprocess.run(command, env=self.environment, text=True, capture_output=True, timeout=30)

    def test_trust_root_matches_application(self):
        self.assertIn(PUBLIC, (ROOT / 'install.sh').read_text())
        self.assertIn(PUBLIC_DER, (ROOT / 'install.ps1').read_text())

    def test_signed_matching_package_installs(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mark.read_text().strip(), '--install')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux dependency setup')
    def test_present_linux_dependencies_are_not_reinstalled(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.package_log.exists(), self.package_log.read_text() if self.package_log.exists() else '')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux dependency setup')
    def test_missing_linux_dependency_installs_only_that_package(self):
        self.bluez_installed = False
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('sudo apt-get install -y bluez\n', self.package_log.read_text())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux dependency setup')
    def test_existing_linux_install_skips_dependency_setup(self):
        self.bluez_installed = False
        installed = self.home / '.local/lib/Sweetmeter/Sweetmeter'
        installed.parent.mkdir(parents=True)
        installed.write_text('#!/bin/sh\nexit 0\n')
        installed.chmod(0o755)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Opening your existing Sweetmeter', result.stdout)
        self.assertFalse(self.package_log.exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux platform guard')
    def test_unsupported_linux_arm_stops_before_install(self):
        self.linux_arch = 'aarch64'
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires x86_64', result.stderr)
        self.assertFalse(self.mark.exists())

    def test_changed_manifest_cannot_execute(self):
        path = self.root / 'manifest.json'
        path.write_bytes(path.read_bytes() + b' ')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.mark.exists())

    def test_changed_archive_cannot_execute(self):
        path = self.root / self.asset
        path.write_bytes(path.read_bytes() + b'changed')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.mark.exists())

    def test_signed_wrong_version_cannot_execute(self):
        self.manifest['version'] = '2026.9.1'
        self.sign()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.mark.exists())
