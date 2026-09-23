"""Firmware trust table: every shipped public key is embedded, selected by key ID."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
BUILD_STATE = {"source_commit": "a" * 40, "source_tree": "b" * 40, "source_fingerprint": "c" * 64, "dirty": False}


def load_build_module():
    spec = importlib.util.spec_from_file_location("firmware_build_keys", ROOT / "scripts/firmware_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_pem(curve=ec.SECP256R1()):
    return ec.generate_private_key(curve).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


class FirmwareKeyTableTests(unittest.TestCase):
    def setUp(self):
        self.module = load_build_module()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "meter/assets/keys").mkdir(parents=True)
        (self.root / "VERSION").write_text("2026.9.14\n")

    def tearDown(self):
        self.temp.cleanup()

    def key(self, name, text):
        (self.root / "meter/assets/keys" / name).write_text(text)

    def test_repository_key_is_embedded(self):
        keys = self.module.trusted_key_table(ROOT)
        self.assertIn("release-1", [key_id for key_id, _ in keys])

    def test_backup_key_is_embedded_in_sorted_order(self):
        self.key("release-2.pem", public_pem())
        self.key("release-1.pem", public_pem())
        target = self.root / "release.h"
        with patch.dict(os.environ, {}, clear=True):
            self.module.generate(self.root, target, build_state=BUILD_STATE)
        text = target.read_text()
        self.assertIn("#define SWEETMETER_TRUSTED_KEY_COUNT 2u", text)
        self.assertIn('SWEETMETER_TRUSTED_KEY_IDS[] = {"release-1", "release-2"}', text)
        self.assertIn("sizeof(SWEETMETER_TRUSTED_KEY_1_PEM)", text)
        self.assertNotIn("PRIVATE KEY", text)
        self.assertNotIn("SWEETMETER_PUBLIC_KEY_PEM", text)

    def test_unsafe_or_ambiguous_keys_are_rejected(self):
        same = public_pem()
        private = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii")
        cases = {
            "private key": [("release-1.pem", private)],
            "wrong curve": [("release-1.pem", public_pem(ec.SECP384R1()))],
            "bad key id": [("Release_1.pem", public_pem())],
            "key id too long": [("release-1234567890.pem", public_pem())],
            "duplicate key": [("release-1.pem", same), ("release-2.pem", same)],
            "no keys": [],
        }
        for label, files in cases.items():
            with self.subTest(label):
                for path in (self.root / "meter/assets/keys").glob("*"):
                    path.unlink()
                for name, text in files:
                    self.key(name, text)
                with self.assertRaises(ValueError):
                    self.module.trusted_key_table(self.root)


if __name__ == "__main__":
    unittest.main()
