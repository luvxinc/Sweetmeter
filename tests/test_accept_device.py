"""Offline acceptance-harness gates and outcome evidence; no device access."""
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from meter.protocol import FirmwareMetadata, ProtocolError, encode_header, OTAState, OTAError, OTAStatus
from scripts import accept_device as acceptance


HOST = "7a1e1000-ff1b-4d9f-a023-0123456789ab"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sweetmeter-offline-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state"
        self.state.mkdir()
        (self.state / "companion.json").write_text(json.dumps({"host_id": HOST}))
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.keys = {"release-1": self.key.public_key()}
        self.frame = self.directory / "example.frame"
        self.frame.write_bytes(bytes([255]) * 4000)

    def image(self, version="2026.9.2", project=b"Sweetmeter"):
        data = bytearray(288)
        data[0:2] = b"\xe9\x01"
        struct.pack_into("<H", data, 12, 9)
        struct.pack_into("<I", data, 28, 256)
        struct.pack_into("<I", data, 32, 0xABCD5432)
        data[48:48 + len(version)] = version.encode()
        data[80:80 + len(project)] = project
        return bytes(data)

    def candidate(self, *, compiled="2026.9.2", signed="2026.9.2", project=b"Sweetmeter"):
        image = self.image(compiled, project)
        metadata = FirmwareMetadata(signed, "2026.9.1", len(image), hashlib.sha256(image).digest())
        header = encode_header(metadata)
        signature = self.key.sign(header, ec.ECDSA(hashes.SHA256()))
        envelope = header + struct.pack("<H", len(signature)) + signature
        path = self.directory / "candidate.bin"
        ota = self.directory / "candidate.ota"
        path.write_bytes(image)
        ota.write_bytes(envelope)
        return path, ota

    def args(self, scenario="baseline", *extra):
        return acceptance.parser().parse_args([
            scenario, "--state-dir", str(self.state), "--output", str(self.directory / "evidence.json"),
            "--expected-running-version", "2026.9.1", "--frame", str(self.frame), *extra,
        ])

    def status(self, **overrides):
        return {
            "protocol": 4, "board": acceptance.BOARD_ID, "firmware": "2026.9.1",
            "selected_host": HOST, "boot_health": "valid", "last_update": "none",
            "ota_target": "", "menu": False, "ota": False, "critical": False,
            **overrides,
        }

    def test_offline_default_never_opens_hardware_or_claims_hardware_pass(self):
        args = self.args()
        with patch.object(acceptance, "exercise", new_callable=AsyncMock) as exercise:
            result = acceptance.main([
                "baseline", "--state-dir", str(self.state), "--output", str(args.output),
                "--expected-running-version", "2026.9.1", "--frame", str(self.frame),
            ])
        exercise.assert_not_called()
        self.assertEqual(result, 0)
        report = json.loads(args.output.read_text())
        self.assertTrue(report["preflight_passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["mode"], "offline-preflight")
        self.assertNotIn(HOST, args.output.read_text())

    def test_execution_requires_explicit_device_exclusivity_and_power(self):
        for extra in (("--execute",), ("--execute", "--device", "Synthetic device")):
            with self.subTest(extra=extra), self.assertRaisesRegex(acceptance.AcceptanceError, "--companion-stopped"):
                acceptance.preflight(self.args("baseline", *extra))
        image, envelope = self.candidate()
        common = ["--execute", "--device", "Synthetic device", "--companion-stopped", "--image", str(image), "--envelope", str(envelope)]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "--confirm-usb-power"):
            acceptance.preflight(self.args("upgrade", *common))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "--allow-reset"):
            acceptance.preflight(self.args("bad-signature", *common, "--confirm-usb-power"))

    def test_invalid_identity_is_never_replaced_and_private_paths_are_required(self):
        before = b'{"host_id":"invalid"}'
        identity_path = self.state / "companion.json"
        identity_path.write_bytes(before)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.identity(self.state)
        self.assertEqual(identity_path.read_bytes(), before)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.private_path(acceptance.ROOT / "state" / "private-report.json")

    def test_preflight_rejects_wrong_compiled_version_and_corrupted_inputs(self):
        image, envelope = self.candidate(compiled="2026.9.3")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Compiled application VERSION"):
            acceptance.load_candidate(image, envelope, "2026.9.1", companion_version="2026.9.1", trusted_keys=self.keys)
        image, envelope = self.candidate()
        accepted = acceptance.load_candidate(image, envelope, "2026.9.1", companion_version="2026.9.1", trusted_keys=self.keys)
        self.assertEqual(str(accepted.metadata.version), "2026.9.2")
        with self.assertRaisesRegex(ProtocolError, "newer"):
            acceptance.load_candidate(image, envelope, "2026.9.2", companion_version="2026.9.1", trusted_keys=self.keys)
        corrupt = bytearray(image.read_bytes())
        corrupt[-1] ^= 1
        image.write_bytes(corrupt)
        with self.assertRaisesRegex(ProtocolError, "digest"):
            acceptance.load_candidate(image, envelope, "2026.9.1", companion_version="2026.9.1", trusted_keys=self.keys)

    def test_rollback_requires_matching_compiled_fixture_and_source(self):
        image, envelope = self.candidate(project=b"Sweetmeter-test-health")
        source = ("a" * 40, "b" * 40)
        fixture = {
            "schema": 1, "kind": "sweetmeter-acceptance-only", "variant": "health-fail",
            "version": "2026.9.2", "root_version": "2026.9.1", "source_commit": source[0], "source_tree": source[1],
            "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "metadata_sha256": hashlib.sha256(envelope.read_bytes()).hexdigest(),
        }
        fixture["build_provenance"] = {
            "schema": 1, "kind": "sweetmeter-firmware-build", "source_commit": source[0],
            "source_tree": source[1], "dirty": False, "source_fingerprint": "d" * 64,
            "version": "2026.9.2", "root_version": "2026.9.1", "test_build": True,
            "variant": "health-fail", "project_name": "Sweetmeter-test-health",
            "image_sha256": fixture["image_sha256"],
        }
        sidecar = self.directory / "fixture.acceptance.json"
        sidecar.write_text(json.dumps(fixture))
        common = dict(scenario="health-fail", companion_version="2026.9.1", trusted_keys=self.keys, source=source)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "sidecar"):
            acceptance.load_candidate(image, envelope, "2026.9.1", **common)
        accepted = acceptance.load_candidate(image, envelope, "2026.9.1", fixture_path=sidecar, **common)
        self.assertEqual(accepted.fixture["variant"], "health-fail")
        for field, value in (("dirty", True), ("test_build", False), ("source_tree", "c" * 40),
                             ("image_sha256", "0" * 64), ("version", "2026.9.3"), ("source_fingerprint", "unknown")):
            with self.subTest(field=field):
                altered = {**fixture, "build_provenance": {**fixture["build_provenance"], field: value}}
                sidecar.write_text(json.dumps(altered))
                with self.assertRaisesRegex(acceptance.AcceptanceError, "build-time provenance"):
                    acceptance.load_candidate(image, envelope, "2026.9.1", fixture_path=sidecar, **common)
        sidecar.write_text(json.dumps({key: value for key, value in fixture.items() if key != "build_provenance"}))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "build-time provenance"):
            acceptance.load_candidate(image, envelope, "2026.9.1", fixture_path=sidecar, **common)
        sidecar.write_text(json.dumps(fixture))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "source commit/tree"):
            acceptance.load_candidate(image, envelope, "2026.9.1", fixture_path=sidecar, **{**common, "source": ("c" * 40, source[1])})
        fixture["variant"] = "normal"
        sidecar.write_text(json.dumps(fixture))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "sidecar"):
            acceptance.load_candidate(image, envelope, "2026.9.1", fixture_path=sidecar, **common)

    def test_baseline_refuses_legacy_wrong_version_host_or_pending_health(self):
        acceptance.validate_initial(self.status(), host=HOST, running="2026.9.1")
        for overrides in (
            {"protocol": 3, "firmware": "QM3.2"}, {"firmware": "2026.9.2"},
            {"selected_host": ""}, {"boot_health": "pending"}, {"menu": True}, {"ota": True},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(acceptance.AcceptanceError):
                acceptance.validate_initial(self.status(**overrides), host=HOST, running="2026.9.1")

    def test_paired_firmware_selection_uses_selected_flag_not_host_id(self):
        paired = {k: v for k, v in self.status().items() if k != "selected_host"}
        paired.update(auth=1, selected=True, secured=True, serial="d405927bbf38", challenge="00" * 16)
        acceptance.validate_initial(paired, host=HOST, running="2026.9.1")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.validate_initial({**paired, "selected": False}, host=HOST, running="2026.9.1")
        self.assertTrue(acceptance.safe_status(paired)["selected"])
        self.assertTrue(acceptance.postcondition({**paired, "firmware": "2026.9.2", "last_update": "success",
                                                   "ota_target": "2026.9.2"}, host=HOST, running="2026.9.1",
                                                  target="2026.9.2", scenario="upgrade"))

    def test_upgrade_and_rollback_require_positive_durable_evidence(self):
        def check(status, scenario):
            return acceptance.postcondition(status, host=HOST, running="2026.9.1", target="2026.9.2", scenario=scenario)
        self.assertFalse(check(self.status(firmware="2026.9.2", last_update="pending", ota_target="2026.9.2"), "upgrade"))
        self.assertFalse(check(self.status(firmware="2026.9.2", boot_health="pending", last_update="success", ota_target="2026.9.2"), "upgrade"))
        self.assertTrue(check(self.status(firmware="2026.9.2", last_update="success", ota_target="2026.9.2"), "upgrade"))
        self.assertFalse(check(self.status(ota_target="2026.9.2"), "health-fail"))
        self.assertFalse(check(self.status(last_update="failed", ota_target="2026.9.2"), "health-fail"))
        self.assertTrue(check(self.status(last_update="rollback", ota_target="2026.9.2"), "health-fail"))
        self.assertFalse(check(self.status(last_update="rollback", ota_target="2026.9.3"), "reset-before-confirm"))

    def test_interruption_occurs_after_acknowledged_bytes_before_commit(self):
        async def run():
            args = self.args()
            report = acceptance.Report(args)
            report.start()
            client = type("Client", (), {"mtu_size": 23, "disconnect": AsyncMock()})()
            transfer = acceptance.ObservedTransfer(client, report, "disconnect", 65536)
            expected = OTAStatus(OTAState.IMAGE, session=transfer.session, offset=65544, total=100000, opcode=ord("d"))
            with patch.object(acceptance.OTATransfer, "exchange", new_callable=AsyncMock, return_value=expected) as exchange:
                with self.assertRaises(acceptance.InjectedDisconnect):
                    await transfer.exchange("data", b"packet", "d", OTAState.IMAGE, 65544, 100000)
            exchange.assert_awaited_once()
            client.disconnect.assert_awaited_once()
            self.assertEqual(transfer.interrupted_at, 65544)
            self.assertFalse(transfer.commit_started)
        asyncio.run(run())

    def test_status_evidence_excludes_machine_identity_and_names(self):
        status = acceptance.safe_status(self.status(name="Personal Computer", selected_host=HOST, future_secret="not for report"))
        encoded = json.dumps(status)
        self.assertNotIn(HOST, encoded)
        self.assertNotIn("Personal", encoded)
        self.assertNotIn("future_secret", encoded)
        self.assertEqual(status["selected_host_fingerprint"], acceptance.fingerprint(HOST))

    def test_previous_rollback_record_cannot_pass_a_new_transfer_failure(self):
        async def run():
            args = self.args("health-fail")
            report = acceptance.Report(args)
            report.start()
            stale = self.status(last_update="rollback", ota_target="2026.9.2")
            session = type("Session", (), {"clock": AsyncMock(), "frame": AsyncMock()})()
            link = type("Link", (), {"open": AsyncMock(return_value=stale), "close": AsyncMock(), "session": session, "client": object()})()
            for committed in (False, True):
                transfer = type("Transfer", (), {
                    "run": AsyncMock(side_effect=RuntimeError("Transport failed")),
                    "states": [], "commit_started": committed,
                })()
                candidate = acceptance.Candidate(b"synthetic image", b"synthetic metadata", None, {})
                with patch.object(acceptance, "Connection", return_value=link), \
                     patch.object(acceptance, "ObservedTransfer", return_value=transfer), \
                     patch.object(acceptance, "reconnect_result", new_callable=AsyncMock) as reconnect:
                    args.confirm_usb_power = True
                    with self.assertRaises(acceptance.AcceptanceError):
                        await acceptance.exercise(args, candidate, HOST, report)
                reconnect.assert_not_called()
                self.assertFalse(report.data["passed"])
        asyncio.run(run())

    def test_existing_evidence_is_not_overwritten(self):
        output = self.directory / "evidence.json"
        output.write_text("Existing private evidence.\n")
        with self.assertRaises(SystemExit):
            acceptance.main([
                "baseline", "--state-dir", str(self.state), "--output", str(output),
                "--expected-running-version", "2026.9.1", "--frame", str(self.frame),
            ])
        self.assertEqual(output.read_text(), "Existing private evidence.\n")


if __name__ == "__main__":
    unittest.main()
