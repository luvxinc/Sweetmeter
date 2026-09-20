"""Cross-language wire fixtures and trust-boundary regression tests (offline)."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import stat
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils

from meter.protocol import *
from meter.version import Version, get_version
from scripts.sign_release import sign_envelope, manifest_bytes, changelog_history, build_release, verify_companion_package, verify_firmware_build
from scripts.generate_signing_key import generate
from scripts.build_provenance import capture_build_state, require_unchanged_build_state
from scripts.firmware_build import finish_build

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
# Deliberately public TEST-ONLY key scalar; never trusted by production firmware.
TEST_KEY = ec.derive_private_key(1, ec.SECP256R1())
TEST_TRUST = {KEY_ID: TEST_KEY.public_key()}


def sample_image(version="2026.9.2", project="Sweetmeter-test"):
    data = bytearray((i * 17 + 3) % 256 for i in range(512))
    data[:24] = bytes(24)
    data[0], data[1] = 0xE9, 1
    struct.pack_into("<H", data, 12, 9)
    struct.pack_into("<II", data, 24, 0x3C000020, 480)
    data[32:288] = bytes(256)
    struct.pack_into("<I", data, 32, 0xABCD5432)
    data[48:80] = version.encode().ljust(32, b"\0")
    data[80:112] = project.encode().ljust(32, b"\0")
    return bytes(data)


def sample_metadata():
    image = sample_image()
    return FirmwareMetadata("2026.9.2", "2026.9.1", len(image), hashlib.sha256(image).digest())


def sample_manifest():
    image = sample_image()
    envelope = sign_envelope(sample_metadata(), TEST_KEY)
    base = "https://github.com/luvxinc/Sweetmeter/releases/download/2026.9.2/"
    return {"schema": 1, "product": "Sweetmeter", "channel": "stable", "version": "2026.9.2",
            "published_at": "2026-09-20T12:00:00Z", "commit": "a" * 40, "key_id": KEY_ID,
            "changes": [{"version": "2026.9.2", "notes": ["Show signed firmware update notes."]},
                        {"version": "2026.9.1", "notes": ["Show quota dashboard."]}],
            "artifacts": [{"kind": "firmware", "version": "2026.9.2", "asset": "firmware.bin",
                           "url": base + "firmware.bin", "size": len(image),
                           "sha256": hashlib.sha256(image).hexdigest(), "board": BOARD_ID,
                           "protocol": PROTOCOL, "minimum_companion": "2026.9.1",
                           "metadata_asset": "firmware.ota", "metadata_url": base + "firmware.ota",
                           "metadata_size": len(envelope), "metadata_sha256": hashlib.sha256(envelope).hexdigest()},
                          {"kind": "companion", "version": "2026.9.2", "asset": "Sweetmeter-macos-arm64.zip",
                           "url": base + "Sweetmeter-macos-arm64.zip", "size": 3,
                           "sha256": hashlib.sha256(b"zip").hexdigest(), "os": "macos", "arch": "arm64"}]}


def sign_raw(data):
    return TEST_KEY.sign(data, ec.ECDSA(hashes.SHA256()))


class VersionTests(unittest.TestCase):
    def test_numeric_order_and_u32_limits(self):
        self.assertGreater(Version.parse("2026.10.1"), Version.parse("2026.9.999"))
        self.assertGreater(Version.parse("2026.9.10"), Version.parse("2026.9.9"))
        self.assertGreater(Version.parse("2027.1.1"), Version.parse("2026.12.4294967295"))
        self.assertEqual(Version.parse("2026.9.4294967295").sequence, 0xFFFFFFFF)

    def test_invalid_versions_and_no_legacy_fallback(self):
        for value in ("2026.09.1", "2026.9.01", "2026.13.1", "2026.0.1", "2026.9.0", "QM3.2",
                      "2026.9.4294967296", "0999.9.1", "2026.9.1\n", "2026.9.١", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Version.parse(value)
        with self.assertRaises(ValueError):
            Version(2026, True, 1)

    def test_single_version_resource_and_missing_resource_failure(self):
        self.assertEqual(get_version(), (ROOT / "VERSION").read_text().strip())
        with tempfile.TemporaryDirectory() as temporary, patch("sys.frozen", True, create=True), patch("sys._MEIPASS", temporary, create=True):
            with self.assertRaises(FileNotFoundError):
                get_version()
            (Path(temporary) / "VERSION").write_text("2026.10.1\n")
            self.assertEqual(get_version(), "2026.10.1")
            (Path(temporary) / "VERSION").write_text("2026.10.01\n")
            with self.assertRaises(ValueError):
                get_version()


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.metadata = sample_metadata()
        self.envelope = sign_envelope(self.metadata, TEST_KEY)

    def test_header_fixture_offsets_and_strict_signature(self):
        header = encode_header(self.metadata)
        self.assertEqual(len(header), 160)
        self.assertEqual(header[:8], b"SWMOTA4\0")
        self.assertEqual(struct.unpack_from("<IIIIIII", header, 64), (2026, 9, 2, 2026, 9, 1, 512))
        self.assertEqual(header[92:124], hashlib.sha256(sample_image()).digest())
        self.assertEqual(header[124:140], b"release-1".ljust(16, b"\0"))
        self.assertEqual(decode_header(header), self.metadata)
        self.assertEqual(verify_envelope(self.envelope, trusted_keys=TEST_TRUST,
                                        current_version="2026.9.1", companion_version="2026.9.1"), self.metadata)

    def test_signed_but_ineligible_metadata(self):
        for changed in (replace(self.metadata, board="different-board"),
                        replace(self.metadata, version="2026.9.1"),
                        replace(self.metadata, version="2026.8.999"),
                        replace(self.metadata, minimum_companion="2026.9.3"),
                        replace(self.metadata, image_size=MAX_IMAGE_SIZE + 1),
                        replace(self.metadata, protocol=5)):
            envelope = sign_raw(encode_header(changed))
            envelope = encode_header(changed) + struct.pack("<H", len(envelope)) + envelope
            with self.subTest(changed=changed), self.assertRaises(ProtocolError):
                verify_envelope(envelope, trusted_keys=TEST_TRUST, current_version="2026.9.1", companion_version="2026.9.1")

    def test_tamper_padding_reserved_and_fixed_string_bounds(self):
        for index in (0, 8, 10, 14, 63, 139, 140, 159):
            header = bytearray(encode_header(self.metadata))
            header[index] ^= 1
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                decode_header(header)
        for index in (64, 88, 92, 162):
            changed = bytearray(self.envelope)
            changed[index] ^= 1
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                verify_envelope(bytes(changed), trusted_keys=TEST_TRUST)
        for value in ("", "a" * 48, "设备", "foo\0bar"):
            with self.assertRaises(ProtocolError):
                replace(self.metadata, board=value)

    def test_envelope_lengths_der_and_untrusted_keys(self):
        for changed in (b"", self.envelope[:-1], self.envelope + b"\0", self.envelope[:160] + b"\x01\x00" + self.envelope[162:]):
            with self.assertRaises(ProtocolError):
                verify_envelope(changed, trusted_keys=TEST_TRUST)
        for signature in (utils.encode_dss_signature(0, 1), utils.encode_dss_signature(1, 0),
                          b"\x30\x81\x06\x02\x01\x01\x02\x01\x01", self.envelope[162:] + b"\0"):
            envelope = self.envelope[:160] + struct.pack("<H", len(signature)) + signature
            with self.assertRaises(ProtocolError):
                verify_envelope(envelope, trusted_keys=TEST_TRUST)
        for keys in ({}, {KEY_ID: ec.generate_private_key(ec.SECP384R1()).public_key()},
                     {KEY_ID: rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()}):
            with self.assertRaises(ProtocolError):
                verify_envelope(self.envelope, trusted_keys=keys)
        # Shipped production key cannot validate the intentionally public fixture key.
        with self.assertRaises(ProtocolError):
            verify_envelope(self.envelope)

    def test_image_versions_are_bound_to_esp_app_descriptor(self):
        self.assertEqual(str(firmware_image_version(sample_image())), "2026.9.2")
        for index in (0, 1, 12, 32):
            changed = bytearray(sample_image())
            changed[index] = 0
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                firmware_image_version(bytes(changed))
        with self.assertRaises(ProtocolError):
            firmware_image_version(sample_image()[:100])
        with self.assertRaises(ProtocolError):
            firmware_image_version(sample_image("1.0.0"))

    def test_public_cross_language_fixture(self):
        fixture = json.loads((FIXTURES / "protocol4.json").read_text())
        meta = verify_envelope(bytes.fromhex(fixture["envelope_hex"]), trusted_keys={KEY_ID: fixture["public_key_pem"].encode()})
        self.assertEqual(meta, self.metadata)
        self.assertEqual(bytes.fromhex(fixture["header_hex"]), encode_header(meta))
        self.assertEqual(hashlib.sha256(bytes.fromhex(fixture["image_hex"])).digest(), meta.image_sha256)
        self.assertEqual(OTAStatus.decode(bytes.fromhex(fixture["status_hex"])).offset, 65538)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = sample_manifest()

    def test_exact_bytes_and_artifact_binding(self):
        raw = manifest_bytes(self.manifest)
        result = verify_manifest(raw, sign_raw(raw), trusted_keys=TEST_TRUST)
        self.assertEqual(result, self.manifest)
        with self.assertRaises(ProtocolError):
            verify_manifest(raw.rstrip(), sign_raw(raw), trusted_keys=TEST_TRUST)
        firmware = select_artifact(result, "firmware", board=BOARD_ID)
        match_firmware_artifact(sample_metadata(), firmware)
        verify_artifact(sample_image(), firmware)
        changed = dict(firmware, size=511)
        with self.assertRaises(ProtocolError):
            match_firmware_artifact(sample_metadata(), changed)
        with self.assertRaises(ProtocolError):
            verify_artifact(sample_image(), changed)
        with self.assertRaises(ProtocolError):
            verify_artifact(bytes(512), firmware)
        self.assertIsNone(select_artifact(result, "companion", os="windows", arch="arm64"))

    def test_duplicate_nested_keys_and_bounds_rejected_even_if_signed(self):
        raw = manifest_bytes(self.manifest)
        bad = raw.replace(b'"schema":1', b'"schema":1,"schema":1')
        nested = raw.replace(b'"kind":"firmware"', b'"kind":"firmware","kind":"firmware"')
        for changed in (bad, nested, b"\xff", b" " * (MAX_MANIFEST_SIZE + 1), b'{"schema":NaN}', b"[]"):
            with self.assertRaises(ProtocolError):
                verify_manifest(changed, sign_raw(changed), trusted_keys=TEST_TRUST)

    def test_signed_invalid_schema_fields_fail(self):
        cases = [dict(self.manifest, schema=True), dict(self.manifest, channel="beta"),
                 dict(self.manifest, product="Other"), dict(self.manifest, commit="abc"),
                 dict(self.manifest, published_at="2026-02-30T12:00:00Z"),
                 dict(self.manifest, changes=[]), dict(self.manifest, artifacts=[])]
        for key, value in (("size", True), ("size", MAX_IMAGE_SIZE + 1), ("sha256", "A" * 64),
                           ("board", "wrong"), ("protocol", True), ("metadata_size", 235),
                           ("minimum_companion", "2026.10.1"),
                           ("metadata_sha256", "xyz"), ("asset", "../../secret"),
                           ("url", "https://github.com/luvxinc/Sweetmeter/releases/download/2026.9.1/firmware.bin")):
            changed = copy.deepcopy(self.manifest)
            changed["artifacts"][0][key] = value
            cases.append(changed)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["artifacts"].append(duplicate["artifacts"][0])
        cases.append(duplicate)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["changes"].append(duplicate["changes"][0])
        cases.append(duplicate)
        for changed in cases:
            raw = json.dumps(changed).encode()
            with self.subTest(changed=changed), self.assertRaises(ProtocolError):
                verify_manifest(raw, sign_raw(raw), trusted_keys=TEST_TRUST)

    def test_url_origin_redirect_and_path_checks(self):
        base = self.manifest["artifacts"][0]["url"]
        self.assertEqual(validate_asset_url(base), base)
        for url in (base.replace("https:", "http:"), base.replace("github.com", "github.com.evil.test"),
                    base.replace("github.com", "attacker@github.com"), base.replace("Sweetmeter/", "Other/"),
                    base + "?redirect=evil", base + "#x", base.replace("firmware.bin", "..%2ffirmware.bin"),
                    base.replace("firmware.bin", "firmware.bin/other")):
            with self.subTest(url=url), self.assertRaises(ProtocolError):
                validate_asset_url(url)
        self.assertEqual(validate_download_url("https://release-assets.githubusercontent.com/test?token=x"),
                         "https://release-assets.githubusercontent.com/test?token=x")
        for url in ("https://example.com/file", "http://objects.githubusercontent.com/file", "https://github.com:443/file"):
            with self.assertRaises(ProtocolError):
                validate_download_url(url)

    def test_streaming_file_verification_and_size_overrun(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "artifact.bin"
            target.write_bytes(sample_image())
            verify_artifact(target, self.manifest["artifacts"][0])
            target.write_bytes(sample_image() + b"extra")
            with self.assertRaises(ProtocolError):
                verify_artifact(target, self.manifest["artifacts"][0])


class WireTests(unittest.TestCase):
    def test_minimum_mtu_and_32bit_offsets(self):
        self.assertEqual(payload_limit(), 12)
        self.assertEqual(payload_limit(metadata=True), 11)
        self.assertEqual(payload_limit(182), 174)
        self.assertEqual(payload_limit(182, metadata=True), 173)
        self.assertEqual(len(ota_begin(0x12345678, 234, "2026.9.1", usb_power=True)), 20)
        self.assertEqual(ota_begin(1, 234, "2026.9.1")[-1], 0)  # no fabricated power acknowledgement
        self.assertEqual(len(ota_fragment(1, 0, bytes(11))), 20)
        data = ota_data(0x12345678, 65538, bytes(12))
        self.assertEqual(len(data), 20)
        self.assertEqual(struct.unpack_from("<II", data), (0x12345678, 65538))
        for call in (lambda: ota_data(0, 0, b"x"), lambda: ota_data(1, 0, bytes(13)),
                     lambda: ota_data(1, 0xFFFFFFFF, b"x"), lambda: ota_fragment(1, 233, bytes(2)),
                     lambda: ota_command("B", 1), lambda: ota_begin(True, 234, "2026.9.1")):
            with self.assertRaises(ProtocolError):
                call()

    def test_status_roundtrip_strictness_and_unknown_error_opcode(self):
        status = OTAStatus(OTAState.IMAGE, OTAError.OK, 0x12345678, 65538, 100000, ord("d"), 3)
        encoded = status.encode()
        self.assertEqual(len(encoded), 20)
        self.assertEqual(OTAStatus.decode(encoded), status)
        self.assertTrue(status.cancellable)
        self.assertTrue(status.signature_verified)
        self.assertEqual(OTAStatus.decode(OTAStatus(OTAState.ERROR, OTAError.MALFORMED, opcode=255).encode()).opcode, 255)
        for offset, value in ((0, 0), (1, 3), (2, 8), (3, 21), (17, 4), (18, 1), (19, 1)):
            data = bytearray(encoded)
            data[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                OTAStatus.decode(data)
        for data in (encoded[:-1], encoded + b"\0"):
            with self.assertRaises(ProtocolError):
                OTAStatus.decode(data)

    def test_registration_fragments_and_identity(self):
        host = "7a1e1000-ff1b-4d9f-a023-0123456789ab"
        body = registration_body(host, "Linux PC")
        self.assertEqual(len(body), 45)
        self.assertEqual(len(registration_begin(1, 2, len(body))), 11)
        rebuilt = bytearray()
        for offset in range(0, len(body), 11):
            packet = registration_fragment(1, offset, body[offset:offset + 11])
            self.assertLessEqual(len(packet), 20)
            rebuilt.extend(packet[9:])
        self.assertEqual(bytes(rebuilt), body)
        self.assertEqual(registration_commit(1), b"K\x01\0\0\0")
        for name in ("", "x" * 21, "测试", "Mac\n"):
            with self.assertRaises(ProtocolError):
                registration_body(host, name)


def firmware_build_record(image):
    return {"schema": 1, "kind": "sweetmeter-firmware-build", "source_commit": "a" * 40,
            "source_tree": "b" * 40, "dirty": False, "source_fingerprint": "c" * 64,
            "version": "2026.9.2", "root_version": "2026.9.2", "test_build": False,
            "variant": "normal", "project_name": "Sweetmeter", "image_sha256": hashlib.sha256(image).hexdigest()}


class BuildProvenanceTests(unittest.TestCase):
    def test_source_snapshot_detects_dirty_and_midbuild_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git("init", "-q")
            git("config", "core.hooksPath", str(root / "absent-hooks"))
            git("config", "user.name", "Offline Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root / ".gitignore").write_text("*.bin\n*.json\n")
            (root / "source.txt").write_text("original\n")
            git("add", ".")
            git("commit", "-qm", "fixture")
            before = capture_build_state(root)
            self.assertFalse(before["dirty"])
            image = root / "firmware.bin"
            image.write_bytes(b"synthetic image")
            record = {**firmware_build_record(image.read_bytes()), **before}
            finish_build(root, image, record)
            sidecar = json.loads(Path(str(image) + ".build.json").read_text())
            self.assertEqual(sidecar["source_commit"], before["source_commit"])
            self.assertEqual(sidecar["image_sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
            (root / "source.txt").write_text("changed after build started\n")
            self.assertTrue(capture_build_state(root)["dirty"])
            with self.assertRaises(ValueError):
                require_unchanged_build_state(before, root)
            with self.assertRaises(ValueError):
                finish_build(root, image, record)

    def test_signer_rejects_stale_dirty_missing_or_mismatched_build_record(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "firmware.bin"
            image.write_bytes(sample_image(project="Sweetmeter"))
            sidecar = Path(str(image) + ".build.json")
            args = dict(version="2026.9.2", root_version="2026.9.2", source_commit="a" * 40, source_tree="b" * 40)
            with self.assertRaises(FileNotFoundError):
                verify_firmware_build(image, **args)
            record = firmware_build_record(image.read_bytes())
            sidecar.write_text(json.dumps(record))
            self.assertEqual(verify_firmware_build(image, **args), record)
            for key, value in (("source_commit", "d" * 40), ("source_tree", "e" * 40), ("dirty", True),
                               ("dirty", 0), ("image_sha256", "f" * 64), ("version", "2026.9.3"),
                               ("test_build", True), ("variant", "health-fail"), ("source_fingerprint", "bad")):
                sidecar.write_text(json.dumps(dict(record, **{key: value})))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    verify_firmware_build(image, **args)
            sidecar.write_text(json.dumps(record))
            image.write_bytes(image.read_bytes()[:-1] + b"tamper")
            with self.assertRaises(ValueError):
                verify_firmware_build(image, **args)

    def test_scons_extra_script_without_file_and_target_dependencies(self):
        class Env(dict):
            def __init__(self):
                super().__init__(PROJECT_DIR=str(ROOT / "firmware"))
                self.dependencies, self.actions = [], []
            def Append(self, **kwargs):
                pass
            def Depends(self, target, dependency):
                self.dependencies.append((target, dependency))
            def AddPostAction(self, target, action):
                self.actions.append((target, action))
        env = Env()
        scope = {"__name__": "scons_extra_script", "env": env, "Import": lambda name: None}
        with tempfile.TemporaryDirectory() as temp:
            # Keep the real generated build files untouched during this regression.
            source = (ROOT / "scripts/firmware_build.py").read_text()
            source = source.replace('generated = generate(build_root, build_state=build_state)',
                                    'generated = generate(build_root, Path(' + repr(temp) + ') / "release.h", build_state=build_state)')
            exec(compile(source, "firmware_build.py", "exec"), scope)
        self.assertEqual(len(env.dependencies), 2)
        self.assertEqual(len(env.actions), 1)


def native_package_fixture(path, *, version="2026.9.2", public_key=None, os_name="linux", arch="x86_64",
                           root=None, omit_entry=False, extra=None, dirty=False, symlinks=False):
    root = root or ("Sweetmeter.app" if os_name == "macos" else "Sweetmeter")
    prefix = root + ("/Contents/Resources/" if os_name == "macos" else "/_internal/")
    entry = root + ("/Contents/MacOS/Sweetmeter" if os_name == "macos" else "/Sweetmeter")
    executable = bytearray(64)
    if os_name == "macos":
        executable[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", executable, 4, 0x100000c if arch == "arm64" else 0x1000007)
    else:
        executable[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", executable, 18, 183 if arch == "arm64" else 62)
    metadata = dict(schema=1, kind="sweetmeter-companion-build", version=version, os=os_name, arch=arch,
                    source_commit="a" * 40, source_tree="b" * 40, source_fingerprint="c" * 64,
                    dirty=dirty, test_build=False, entrypoint=entry)
    if public_key is None:
        public_key = (ROOT / "meter/assets/keys/release-1.pem").read_bytes()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(prefix + "VERSION", version + "\n")
        archive.writestr(prefix + "meter/assets/keys/release-1.pem", public_key)
        archive.writestr(prefix + "build-metadata.json", json.dumps(metadata))
        if not omit_entry:
            info = zipfile.ZipInfo(entry)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, executable)
        if extra:
            archive.writestr(extra, "must reject")
        if symlinks:
            for suffix, target in (("VERSION", "../Resources/VERSION"),
                                   ("meter/assets/keys/release-1.pem", "../../../../Resources/meter/assets/keys/release-1.pem")):
                link = zipfile.ZipInfo(root + "/Contents/Frameworks/" + suffix)
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, target)
    receipt = dict(metadata, artifact_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                   executable_sha256=hashlib.sha256(executable).hexdigest())
    Path(str(path) + ".build.json").write_text(json.dumps(receipt))


class SigningToolsTests(unittest.TestCase):
    def test_external_key_generation_preserves_existing_key_and_refuses_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            private, public = Path(temp) / "private.pem", Path(temp) / "public.pem"
            first = generate(private, public)
            self.assertEqual(generate(private, public), first)
            if os.name != "nt":
                self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            self.assertIn(b"BEGIN PUBLIC KEY", public.read_bytes())
            self.assertNotIn(b"PRIVATE KEY", public.read_bytes())
            public.write_bytes(b"different public trust")
            with self.assertRaises(ValueError):
                generate(private, public)
            private.unlink()
            with self.assertRaises(ValueError):
                generate(private, public)
            self.assertFalse(private.exists())

    def test_companion_bundle_version_and_trust_checked_before_signing(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "app.zip"
            public = (ROOT / "meter/assets/keys/release-1.pem").read_bytes()
            for version, key, accepted in (("2026.9.2", public, True), ("2026.9.1", public, False),
                                           ("2026.9.2", b"wrong trust", False)):
                native_package_fixture(package, version=version, public_key=key)
                args = dict(os_name="linux", arch="x86_64", source_commit="a" * 40, source_tree="b" * 40)
                if accepted:
                    verify_companion_package(package, "2026.9.2", **args)
                else:
                    with self.assertRaises(ValueError):
                        verify_companion_package(package, "2026.9.2", **args)

    def test_macos_resource_symlinks_are_not_mistaken_for_resource_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "macos.zip"
            native_package_fixture(package, os_name="macos", arch="arm64", symlinks=True)
            verify_companion_package(package, "2026.9.2", os_name="macos", arch="arm64",
                                     source_commit="a" * 40, source_tree="b" * 40)

    def test_native_signer_rejects_missing_executable_wrong_root_traversal_and_arch(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "app.zip"
            cases = ({"omit_entry": True}, {"root": "Arbitrary"}, {"extra": "../../unexpected.txt"},
                     {"arch": "arm64"}, {"dirty": True})
            for case in cases:
                native_package_fixture(package, **case)
                with self.subTest(case=case), self.assertRaises((ValueError, KeyError)):
                    verify_companion_package(package, "2026.9.2", os_name="linux", arch="x86_64",
                                             source_commit="a" * 40, source_tree="b" * 40)
            native_package_fixture(package)
            with self.assertRaises(ValueError):
                verify_companion_package(package, "2026.9.2", os_name="linux", arch="x86_64",
                                         source_commit="f" * 40, source_tree="b" * 40)
            receipt = Path(str(package) + ".build.json")
            record = json.loads(receipt.read_text())
            receipt.write_text(json.dumps(dict(record, artifact_sha256="0" * 64)))
            with self.assertRaises(ValueError):
                verify_companion_package(package, "2026.9.2", os_name="linux", arch="x86_64",
                                         source_commit="a" * 40, source_tree="b" * 40)

    def test_release_builder_refuses_test_image_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as temp:
            firmware, output = Path(temp) / "firmware.bin", Path(temp) / "release"
            args = dict(firmware=firmware, companions=[], minimum_companion="2026.9.1", output=output,
                        key=TEST_KEY, commit="a" * 40, version="2026.9.2",
                        changes=[{"version": "2026.9.2", "notes": ["Test offline signed release builder."]}],
                        published_at="2026-09-20T12:00:00Z")
            firmware.write_bytes(sample_image())
            with self.assertRaises(ValueError):
                build_release(**args)
            self.assertFalse(output.exists())
            firmware.write_bytes(sample_image(project="Sweetmeter"))
            record = firmware_build_record(firmware.read_bytes())
            Path(str(firmware) + ".build.json").write_text(json.dumps(record))
            # This test-only trust override never modifies the shipped public resource.
            state = {key: record[key] for key in ("source_commit", "source_tree", "dirty", "source_fingerprint")}
            with patch("scripts.sign_release.trusted_keys", return_value=TEST_TRUST), patch("meter.protocol.trusted_keys", return_value=TEST_TRUST), patch("scripts.sign_release.capture_build_state", return_value=state), patch("scripts.sign_release.get_version", return_value="2026.9.2"):
                result = build_release(**args)
                raw = (output / "manifest.json").read_bytes()
                self.assertEqual(verify_manifest(raw, (output / "manifest.json.sig").read_bytes()), result)
                with self.assertRaises(FileExistsError):
                    build_release(**args)
                self.assertEqual((output / "manifest.json").read_bytes(), raw)


class BuildInjectionTests(unittest.TestCase):
    def test_guarded_fixture_overrides_and_public_only_include(self):
        spec = importlib.util.spec_from_file_location("firmware_build_test", ROOT / "scripts/firmware_build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # This unit test clears the environment to exercise only override flags;
        # provenance capture has its own integration tests and needs Git's OS env.
        build_state = {"source_commit": "a" * 40, "source_tree": "b" * 40,
                       "source_fingerprint": "c" * 64, "dirty": False}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "release.h"
            with patch.dict(os.environ, {}, clear=True):
                module.generate(ROOT, target, build_state=build_state)
                text = target.read_text()
                self.assertIn(f'#define SWEETMETER_VERSION "{get_version()}"', text)
                self.assertIn('#define SWEETMETER_PROJECT_NAME "Sweetmeter"', text)
                self.assertNotIn("PRIVATE KEY", text)
            with patch.dict(os.environ, {"SWEETMETER_TEST_VERSION": "2026.9.99"}, clear=True), self.assertRaises(ValueError):
                module.generate(ROOT, target, build_state=build_state)
            with patch.dict(os.environ, {"SWEETMETER_TEST_BUILD": "1", "SWEETMETER_TEST_VERSION": "2026.9.99",
                                         "SWEETMETER_TEST_VARIANT": "health-fail"}, clear=True):
                module.generate(ROOT, target, build_state=build_state)
                text = target.read_text()
                self.assertIn('#define SWEETMETER_PROJECT_NAME "Sweetmeter-test-health"', text)
                self.assertIn("#define SWEETMETER_TEST_HEALTH_FAIL 1", text)
                self.assertIn("#define SWEETMETER_TEST_RESET_BEFORE_CONFIRM 0", text)


if __name__ == "__main__":
    unittest.main()
