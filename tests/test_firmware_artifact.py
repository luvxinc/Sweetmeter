import hashlib
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import firmware_artifact as artifact


class FirmwareArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.archive = self.root / 'accepted.zip'
        self.source = self.root / 'source'
        for name in artifact.REQUIRED | {str(artifact.BUILD / 'src/main.cpp.o')}:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'accepted exact bytes: ' + name.encode())
        artifact.create(self.archive, self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_reuses_image_and_application_objects_without_building(self):
        destination = self.root / 'checkout'
        destination.mkdir()
        (destination / 'VERSION').write_text('2026.9.2\n')
        image = (self.source / artifact.BUILD / 'firmware.bin').read_bytes()
        digest = hashlib.sha256(image).hexdigest()
        with patch.object(artifact.subprocess, 'check_output', side_effect=['a' * 40, 'b' * 40]), \
                patch.object(artifact, 'verify_firmware_build') as verify:
            artifact.restore(self.archive, digest, 'a' * 40, destination)
        self.assertEqual((destination / artifact.BUILD / 'firmware.bin').read_bytes(), image)
        self.assertEqual((destination / artifact.BUILD / 'src/main.cpp.o').read_bytes(),
                         (self.source / artifact.BUILD / 'src/main.cpp.o').read_bytes())
        verify.assert_called_once_with(destination / artifact.BUILD / 'firmware.bin',
            version='2026.9.2', root_version='2026.9.2', source_commit='a' * 40, source_tree='b' * 40)

    def test_digest_mismatch_writes_nothing(self):
        destination = self.root / 'empty'
        destination.mkdir()
        with patch.object(artifact.subprocess, 'check_output', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'accepted image digest'):
                artifact.restore(self.archive, '0' * 64, 'a' * 40, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_missing_inputs_traversal_links_and_unrelated_files_rejected(self):
        for name in ['../outside.o', 'firmware/.generated/../../escape.h', 'README.md',
                     'firmware/.pio/build/crowpanel213/private.pem']:
            self.assertFalse(artifact.allowed(name))
        for malicious in ('link', 'traversal', 'missing'):
            with self.subTest(malicious=malicious):
                bad = self.root / (malicious + '.zip')
                with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(bad, 'w') as target:
                    for entry in source.infolist():
                        if malicious == 'missing' and entry.filename.endswith('firmware.elf'):
                            continue
                        target.writestr(entry, source.read(entry))
                    if malicious == 'link':
                        entry = zipfile.ZipInfo(str(artifact.BUILD / 'link.o'))
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                        target.writestr(entry, '../outside')
                    if malicious == 'traversal':
                        target.writestr(str(artifact.BUILD / '../outside.o'), b'bad')
                with zipfile.ZipFile(bad) as archive, self.assertRaises(ValueError):
                    artifact.check_entries(archive)


if __name__ == '__main__':
    unittest.main()
