#!/usr/bin/env python3
"""Explicit hardware acceptance, using the production BLE and OTA transports.

Without --execute this performs local preflight only. It never starts/stops a
companion, fetches account data, flashes over USB, erases NVS, or publishes files.
Reports and backups belong outside the repository.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meter.bluetooth import (HOST_PATTERN, STATUS_UUID, PairingRejected, PairingStore, Session,
                             authorize_link, parse_status)
from meter.instance_lock import InstanceLock
from meter.ota import OTATransfer
from meter.protocol import (
    BOARD_ID, FirmwareMetadata, OTAError, OTAState, OTAStatus,
    firmware_image_version, verify_artifact, verify_envelope,
)
from meter.version import Version, get_version
from scripts.build_provenance import read_record

NEGATIVE = {"bad-signature", "bad-digest", "cancel", "disconnect"}
ROLLBACK = {"health-fail", "reset-before-confirm"}
SCENARIOS = ("baseline", *sorted(NEGATIVE), "upgrade", *sorted(ROLLBACK))
PROJECTS = {
    "normal": b"Sweetmeter-test",
    "health-fail": b"Sweetmeter-test-health",
    "reset-before-confirm": b"Sweetmeter-test-reset",
}


class AcceptanceError(RuntimeError):
    pass


class InjectedDisconnect(RuntimeError):
    pass


def private_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise AcceptanceError("Keep state, evidence and full-flash backups outside the repository.")
    return path


def read_json(path: Path, limit=65536):
    raw = path.read_bytes()
    if len(raw) > limit:
        raise AcceptanceError("JSON input exceeds its bounded size.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AcceptanceError("Expected a JSON object.")
    return value


def selected_by(status: dict, host: str) -> bool:
    """Legacy firmware names its selected host; paired firmware only says whether one is selected.

    For paired firmware the selection is proven later by the authenticated hello.
    """
    if status.get("auth") == 1:
        return status.get("selected") is True
    return status.get("selected_host") == host


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def identity(state_dir: Path) -> str:
    # Never silently generate a new identity or overwrite the selected user's NVS.
    host = read_json(private_path(state_dir) / "companion.json", 4096).get("host_id")
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host):
        raise AcceptanceError("Use an existing companion state directory with a valid selected host ID.")
    return host


def companion_lock(state_dir: Path):
    """Hold the companion's own instance lock: proves it is stopped and keeps it so.

    The harness reads and, after a legacy-to-pairing upgrade, updates that
    companion's pairing state exactly as the companion would, so the state
    directory must belong to a companion that is not running.
    """
    try:
        return InstanceLock(private_path(state_dir) / "meter.lock")
    except OSError as exc:
        raise AcceptanceError("The companion using --state-dir is still running; quit it before acceptance.") from exc


def safe_status(status: dict) -> dict:
    fields = (
        "protocol", "firmware", "board", "battery_percent", "battery_mv", "interval",
        "critical", "charge_state", "clock_synced", "menu", "ota", "boot_health",
        "last_update", "ota_target",
    )
    value = {key: status[key] for key in fields if key in status}
    value["selected_host_fingerprint"] = fingerprint(status.get("selected_host", ""))
    value["selected"] = bool(status.get("selected")) if status.get("auth") == 1 else bool(status.get("selected_host"))
    return value


def current_source() -> tuple[str, str]:
    def git(*args):
        result = subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()
    return git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


@dataclass(frozen=True)
class Candidate:
    image: bytes
    envelope: bytes
    metadata: FirmwareMetadata
    fixture: dict | None


def load_candidate(image_path: Path, envelope_path: Path, running: str, *,
                   fixture_path: Path | None = None, scenario="upgrade", trusted_keys=None,
                   companion_version: str | None = None, source: tuple[str, str] | None = None) -> Candidate:
    companion = companion_version or get_version()
    if envelope_path.stat().st_size > 234:
        raise AcceptanceError("OTA envelope exceeds the protocol limit.")
    envelope = envelope_path.read_bytes()
    metadata = verify_envelope(envelope, current_version=running, companion_version=companion,
                               trusted_keys=trusted_keys)
    if image_path.stat().st_size != metadata.image_size:
        raise AcceptanceError("Application size does not match the signed metadata.")
    image = image_path.read_bytes()
    verify_artifact(image, {"size": metadata.image_size, "sha256": metadata.image_sha256.hex()})
    if firmware_image_version(image) != metadata.version:
        raise AcceptanceError("Compiled application VERSION differs from the signed target version.")
    project = image[80:112].split(b"\0", 1)[0]
    fixture = None
    if fixture_path:
        fixture = read_record(fixture_path)
        variant = scenario if scenario in ROLLBACK else "normal"
        expected = {
            "schema": 1, "kind": "sweetmeter-acceptance-only", "variant": variant,
            "version": str(metadata.version), "root_version": companion,
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "metadata_sha256": hashlib.sha256(envelope).hexdigest(),
        }
        if any(fixture.get(key) != value for key, value in expected.items()):
            raise AcceptanceError("Fixture sidecar does not match the source version, variant or artifact hashes.")
        commit, tree = source or current_source()
        if fixture.get("source_commit") != commit or fixture.get("source_tree") != tree:
            raise AcceptanceError("Fixture was not built from this recorded source commit/tree.")
        if project != PROJECTS[variant]:
            raise AcceptanceError("Compiled fixture project does not match the requested test variant.")
        provenance = fixture.get("build_provenance")
        expected_build = {
            "schema": 1, "kind": "sweetmeter-firmware-build", "source_commit": commit,
            "source_tree": tree, "version": str(metadata.version), "root_version": companion,
            "variant": variant, "project_name": PROJECTS[variant].decode("ascii"),
            "image_sha256": expected["image_sha256"],
        }
        if (not isinstance(provenance, dict)
                or any(provenance.get(key) != value for key, value in expected_build.items())
                or provenance.get("dirty") is not False
                or provenance.get("test_build") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_fingerprint", "")))):
            raise AcceptanceError("Fixture build-time provenance is missing, dirty or inconsistent with its artifacts/source.")
    elif scenario in ROLLBACK or project != b"Sweetmeter":
        raise AcceptanceError("Test images require their acceptance sidecar; rollback cases require the matching compiled test variant.")
    return Candidate(image, envelope, metadata, fixture)


def validate_initial(status: dict, *, host: str, running: str) -> None:
    if status.get("protocol") != 4:
        raise AcceptanceError("Protocol 4 USB bootstrap is required; legacy QM versions are not numeric releases.")
    if status.get("board") != BOARD_ID:
        raise AcceptanceError("Connected board does not match the supported hardware.")
    if Version.parse(status.get("firmware")) != Version.parse(running):
        raise AcceptanceError("Device running VERSION differs from --expected-running-version.")
    if not selected_by(status, host):
        raise AcceptanceError("Physically select this existing companion before acceptance; the harness will not change selection.")
    if status.get("menu") or status.get("ota") or status.get("critical"):
        raise AcceptanceError("Close discovery and finish other operations before acceptance.")
    if status.get("boot_health") != "valid":
        raise AcceptanceError("Baseline firmware has not confirmed healthy startup.")


def postcondition(status: dict, *, host: str, running: str, target: str, scenario: str) -> bool:
    if status.get("protocol") != 4 or status.get("board") != BOARD_ID or not selected_by(status, host):
        return False
    if status.get("boot_health") != "valid" or status.get("ota"):
        return False
    if scenario == "upgrade":
        return (status.get("firmware") == target and status.get("last_update") == "success"
                and status.get("ota_target") == target)
    if scenario in ROLLBACK:
        return (status.get("firmware") == running and status.get("last_update") == "rollback"
                and status.get("ota_target") == target)
    return status.get("firmware") == running


class Report:
    def __init__(self, args):
        self.data = {
            "schema": 1, "started_at": datetime.now(timezone.utc).isoformat(),
            "scenario": args.scenario, "mode": "hardware" if args.execute else "offline-preflight",
            "companion_version": get_version(), "expected_running_version": args.expected_running_version,
            "device_fingerprint": fingerprint(args.device or ""), "events": [], "passed": False,
            "optical_display_verified": False, "battery_validated": False,
        }

    def event(self, kind, **data):
        self.data["events"].append({"elapsed_seconds": round(time.monotonic() - self.started, 3), "kind": kind, **data})

    def start(self):
        self.started = time.monotonic()

    def write(self, path: Path):
        path = private_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        fd, temporary = tempfile.mkstemp(prefix=".sweetmeter-evidence-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class ObservedTransfer(OTATransfer):
    """Instrument the production transfer; never replace its packet/ACK logic."""
    def __init__(self, client, report, scenario, threshold):
        super().__init__(client, lambda event: None)
        self.report, self.scenario, self.threshold = report, scenario, threshold
        self.interrupted_at = None
        self.states = []
        self.last_status = None

    def record_status(self, status):
        if status.session == self.session:
            # Keep transitions/errors, not hundreds of thousands of chunks.
            signature = (status.state, status.error, status.opcode)
            if signature != self.last_status:
                self.states.append(status)
                self.report.event("ota", state=status.state.name, error=status.error.name,
                                  offset=status.offset, total=status.total, opcode=status.opcode)
                self.last_status = signature

    def notification(self, characteristic, raw):
        try:
            self.record_status(OTAStatus.decode(bytes(raw)))
        except ValueError:
            self.report.event("malformed_ota_notification")
        super().notification(characteristic, raw)

    def _matches(self, status, opcode, state, offset, total):
        # A lost notification may be recovered by the production read fallback;
        # that typed state is evidence too, including an explicit device error.
        self.record_status(status)
        return super()._matches(status, opcode, state, offset, total)

    async def exchange(self, characteristic, packet, opcode, state, offset, total, timeout=10):
        result = await super().exchange(characteristic, packet, opcode, state, offset, total, timeout)
        if opcode == "d" and self.interrupted_at is None and self.scenario in {"cancel", "disconnect"} and offset >= self.threshold:
            self.interrupted_at = offset
            self.report.event("injected_interruption", action=self.scenario, acknowledged_bytes=offset, total=total)
            if self.scenario == "cancel":
                self.cancel.set()
            else:
                await self.client.disconnect()
                raise InjectedDisconnect("Deliberate precommit disconnect")
        return result


class Connection:
    def __init__(self, device, host, report, state_dir=None):
        self.device, self.host, self.report = device, host, report
        self.state_dir = state_dir
        self.client = self.session = None

    async def connect(self) -> dict:
        """Connect and read status; never selects or registers a host."""
        from bleak import BleakClient
        self.client = BleakClient(self.device, timeout=15)
        await asyncio.wait_for(self.client.connect(), 20)
        status = await self.status()
        if not selected_by(status, self.host):
            raise AcceptanceError("Device selection changed; refusing to select or register a host.")
        return status

    async def authorize(self, status: dict, *, allow_migration=False) -> str:
        """Authorize this link as the stopped companion would, with its own state.

        Only the companion's pairing logic (``authorize_link``) and its
        PairingStore in --state-dir are used. The legacy H/Y migration, which
        stores a fresh secret, is allowed only after the scenario's upgrade
        from pre-secret firmware (``allow_migration``).
        """
        self.session = Session(self.client, self.host, "Acceptance", self.record_event,
                               protocol=status["protocol"])
        await self.session.subscribe()
        if status.get("auth") != 1:
            await self.session.hello()
            return "legacy"
        if not status.get("secured") and not allow_migration:
            raise AcceptanceError("Let the companion finish pairing this meter before acceptance.")
        store = PairingStore(private_path(self.state_dir))
        address = getattr(self.device, "address", str(self.device))
        try:
            return await authorize_link(self.session, store, address, status)
        except PairingRejected as exc:
            raise AcceptanceError("The selected meter did not accept this companion's pairing state.") from exc

    async def open(self):
        status = await self.connect()
        await self.authorize(status)
        return status

    def record_event(self, event):
        if event.get("event") == "ack":
            self.report.event("frame_ack", **{k: event[k] for k in ("sequence", "crc32", "ack")})

    async def status(self):
        return parse_status(await asyncio.wait_for(self.client.read_gatt_char(STATUS_UUID), 10))

    async def close(self):
        if self.client is not None and self.client.is_connected:
            await asyncio.wait_for(self.client.disconnect(), 10)


async def serial_reset(port_name, expected, report):
    import serial
    stream = serial.Serial(None, baudrate=115200, timeout=.1)
    stream.dtr = stream.rts = False
    stream.port = port_name
    stream.open()
    try:
        stream.reset_input_buffer()
        stream.rts = True
        await asyncio.sleep(.1)
        stream.rts = False
        report.event("en_reset", reason="verify previous boot selection after rejected or interrupted OTA")
        until = asyncio.get_running_loop().time() + 45
        prefix = f"READY SWEETMETER {expected} protocol=4 "
        while asyncio.get_running_loop().time() < until:
            line = (await asyncio.to_thread(stream.readline)).decode("utf-8", "replace").strip()
            if "Guru Meditation" in line or "panic'ed" in line:
                raise AcceptanceError("Firmware panic after diagnostic reset.")
            if line.startswith(prefix):
                report.event("serial_ready", firmware=expected)
                return
        raise AcceptanceError("No matching serial READY marker after EN reset; old-boot preservation is unproven.")
    finally:
        stream.close()


async def reconnect_result(args, host, target, report):
    deadline = asyncio.get_running_loop().time() + args.reconnect_timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        link = Connection(args.device, host, report, args.state_dir)
        try:
            # The postcondition is proven by the status read itself: a legacy
            # meter upgraded to pairing firmware still needs its secret, which
            # is provisioned only afterwards, exactly as the companion would.
            status = await link.connect()
            last = safe_status(status)
            if postcondition(status, host=host, running=args.expected_running_version,
                             target=target, scenario=args.scenario):
                report.event("confirmed_boot", status=last)
                method = await link.authorize(status, allow_migration=args.scenario == "upgrade")
                report.event("authorized_after_boot", method=method)
                return
        except (OSError, TimeoutError, ConnectionError):
            pass
        except Exception as exc:
            if isinstance(exc, AcceptanceError):
                raise
            report.event("reconnect_attempt", error_type=type(exc).__name__)
        finally:
            await link.close()
        await asyncio.sleep(2)
    report.event("unconfirmed_boot", last_status=last)
    raise AcceptanceError("Expected firmware/health/update outcome was not confirmed before reconnect deadline.")


async def exercise(args, candidate: Candidate | None, host: str, report: Report):
    link = Connection(args.device, host, report, args.state_dir)
    try:
        status = await link.open()
        validate_initial(status, host=host, running=args.expected_running_version)
        report.event("baseline_status", status=safe_status(status))
        await link.session.clock()
        if args.frame:
            frame = args.frame.read_bytes()
            if len(frame) != 4000:
                raise AcceptanceError("A dashboard frame must be exactly 4000 bytes.")
            report.event("submitted_frame", crc32=f"{zlib.crc32(frame):08x}")
            await link.session.frame(frame)
        if args.scenario == "baseline":
            fresh = await link.status()
            if not fresh.get("clock_synced"):
                raise AcceptanceError("Clock synchronization was not confirmed.")
            report.data["passed"] = True
            return

        assert candidate is not None
        image, envelope = candidate.image, candidate.envelope
        if args.scenario == "bad-signature":
            envelope = envelope[:-1] + bytes([envelope[-1] ^ 1])
        if args.scenario == "bad-digest":
            image = bytearray(image)
            image[len(image) // 2] ^= 1
            image = bytes(image)
        with tempfile.TemporaryDirectory(prefix="sweetmeter-acceptance-") as temporary:
            staged = Path(temporary) / "candidate.bin"
            staged.write_bytes(image)
            transfer = ObservedTransfer(link.client, report, args.scenario, args.interrupt_after_bytes)
            try:
                await transfer.run(staged, envelope, get_version(), usb_power=args.confirm_usb_power)
            except Exception as exc:
                # The typed notification and observed next boot determine the
                # result; a transport exception alone proves neither failure nor success.
                report.event("transfer_exception", error_type=type(exc).__name__, commit_started=transfer.commit_started)
            states = transfer.states
            if args.scenario in {"upgrade", *ROLLBACK} and not transfer.commit_started:
                raise AcceptanceError("No finish/commit attempt occurred; reconnecting an existing image is not an OTA test.")
            if args.scenario in ROLLBACK and not any(s.state == OTAState.REBOOTING for s in states):
                raise AcceptanceError("Candidate boot selection was not acknowledged in this session; a previous rollback record is not new rollback evidence.")
            if args.scenario in NEGATIVE:
                if any(s.state == OTAState.REBOOTING for s in states):
                    raise AcceptanceError("A rejected/interrupted candidate reached boot selection.")
                expected_error = {"bad-signature": OTAError.SIGNATURE, "bad-digest": OTAError.DIGEST}.get(args.scenario)
                if expected_error and not any(s.error == expected_error for s in states):
                    raise AcceptanceError("Device did not report the specific expected rejection error.")
                if args.scenario in {"cancel", "disconnect"} and transfer.interrupted_at is None:
                    raise AcceptanceError("The requested interruption threshold was never reached.")
                if args.scenario == "cancel" and not any(s.state == OTAState.CANCELLED and s.error == OTAError.OK for s in states):
                    raise AcceptanceError("Device cancellation was not acknowledged.")
        await link.close()
        if args.scenario in NEGATIVE:
            await serial_reset(args.serial_port, args.expected_running_version, report)
        await reconnect_result(args, host, str(candidate.metadata.version), report)
        report.data["passed"] = True
    finally:
        await link.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", choices=SCENARIOS)
    p.add_argument("--execute", action="store_true", help="Actually connect/write; otherwise run local preflight only")
    p.add_argument("--device", help="Explicit device BLE address/UUID; never scan/select a random device")
    p.add_argument("--state-dir", type=Path, required=True, help="Existing companion state outside the checkout")
    p.add_argument("--expected-running-version", required=True, type=lambda s: str(Version.parse(s)))
    p.add_argument("--output", type=Path, required=True, help="Private JSON evidence path outside the checkout")
    p.add_argument("--frame", type=Path, help="An explicit 4000-byte frame; required for baseline ACK verification")
    p.add_argument("--image", type=Path)
    p.add_argument("--envelope", type=Path)
    p.add_argument("--fixture", type=Path, help="Matching acceptance-only fixture sidecar for any test build")
    p.add_argument("--companion-stopped", action="store_true", help="Acknowledge exclusive device ownership; harness never stops an app")
    p.add_argument("--confirm-usb-power", action="store_true", help="Explicit USB-power attestation; no software measurement is claimed")
    p.add_argument("--serial-port", help="Explicit diagnostic port for post-rejection EN-reset verification")
    p.add_argument("--allow-reset", action="store_true", help="Allow EN reset after rejection/interruption; no flash erase/write")
    p.add_argument("--interrupt-after-bytes", type=int, default=65536)
    p.add_argument("--reconnect-timeout", type=int, default=120)
    return p


def preflight(args):
    private_path(args.output)
    host = identity(args.state_dir)
    if not 1 <= args.reconnect_timeout <= 120:
        raise AcceptanceError("Reconnect timeout must be between 1 and 120 seconds.")
    if args.execute and (not args.device or not args.companion_stopped):
        raise AcceptanceError("Hardware execution requires --device and --companion-stopped.")
    if args.frame and args.frame.stat().st_size != 4000:
        raise AcceptanceError("Expected a 4000-byte frame.")
    if args.scenario == "baseline":
        if not args.frame:
            raise AcceptanceError("Baseline requires an explicit frame to verify the displayed CRC ACK.")
        return host, None
    if not args.image or not args.envelope:
        raise AcceptanceError("OTA cases require a locally supplied signed envelope and application image.")
    if args.execute and not args.confirm_usb_power:
        raise AcceptanceError("Explicit --confirm-usb-power is required for an OTA test.")
    if args.execute and args.scenario in NEGATIVE and (not args.serial_port or not args.allow_reset):
        raise AcceptanceError("Rejected/interrupted OTA tests require --serial-port and --allow-reset to prove the old image still boots.")
    candidate = load_candidate(args.image, args.envelope, args.expected_running_version,
                               fixture_path=args.fixture, scenario=args.scenario)
    if args.scenario in {"cancel", "disconnect"} and not 1 <= args.interrupt_after_bytes < len(candidate.image) - 182:
        raise AcceptanceError("Interruption threshold must leave at least one full GATT packet before image completion.")
    return host, candidate


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        args.output = private_path(args.output)
        if args.output.exists():
            raise AcceptanceError("Choose a new evidence filename; existing reports/backups are never overwritten.")
    except AcceptanceError as exc:
        command.error(str(exc))
    report = Report(args)
    report.start()
    try:
        host, candidate = preflight(args)
        report.data["selected_host_fingerprint"] = fingerprint(host)
        if candidate:
            report.data["candidate"] = {
                "version": str(candidate.metadata.version), "image_sha256": candidate.metadata.image_sha256.hex(),
                "image_size": candidate.metadata.image_size,
                "fixture_variant": candidate.fixture.get("variant") if candidate.fixture else None,
            }
        report.data["preflight_passed"] = True
        if args.execute:
            lock = companion_lock(args.state_dir)
            try:
                asyncio.run(exercise(args, candidate, host, report))
            finally:
                lock.close()
        else:
            report.event("offline_only", note="No BLE/USB/account access performed; hardware acceptance remains untested.")
        return_code = 0
    except Exception as exc:
        report.data["error_type"] = type(exc).__name__
        report.data["error"] = str(exc) if isinstance(exc, (AcceptanceError, ValueError)) else "See local diagnostic exception type; no transport details were persisted."
        return_code = 1
    finally:
        report.write(args.output)
    print(json.dumps({"mode": report.data["mode"], "preflight_passed": report.data.get("preflight_passed", False),
                      "hardware_passed": report.data["passed"], "report": str(private_path(args.output))}))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
