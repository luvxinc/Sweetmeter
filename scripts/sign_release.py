#!/usr/bin/env python3
"""Build signed GitHub release assets from tested native packages and firmware.

Private keys are read from external files, never command text or manifests.
API: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from meter.protocol import (BOARD_ID, KEY_ID, PROTOCOL, MAX_IMAGE_SIZE, MAX_MANIFEST_SIZE,
                            FirmwareMetadata, ProtocolError, encode_header, firmware_image_version,
                            trusted_keys, validate_manifest, verify_artifact, verify_envelope, verify_manifest)
from meter.version import Version, get_version
from scripts.build_provenance import capture_build_state, read_record


def load_private_key(path):
    path = Path(path)
    if path.is_symlink() or path.resolve().is_relative_to(ROOT):
        raise ValueError("Signing key must be a regular file outside the repository")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
        raise ValueError("Private signing key must have 0600 permissions")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Signing key must be ECDSA P-256")
    return key


def sign_envelope(metadata, key):
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Signing key must be ECDSA P-256")
    header = encode_header(metadata)
    signature = key.sign(header, ec.ECDSA(hashes.SHA256()))
    envelope = header + struct.pack("<H", len(signature)) + signature
    verify_envelope(envelope, trusted_keys={metadata.key_id: key.public_key()}, board=metadata.board)
    return envelope


def manifest_bytes(manifest):
    validate_manifest(manifest)
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_MANIFEST_SIZE:
        raise ValueError("Manifest history exceeds the protocol size bound")
    return raw


def changelog_history(path, version):
    entries = []
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"## \[([^]]+)\] - [0-9]{4}-[0-9]{2}-[0-9]{2}", line)
        if match:
            current = {"version": str(Version.parse(match[1])), "notes": []}
            entries.append(current)
        elif current is not None and line.startswith("- "):
            current["notes"].append(line[2:])
    entries = [entry for entry in entries if Version.parse(entry["version"]) <= Version.parse(version)]
    entries.sort(key=lambda entry: Version.parse(entry["version"]), reverse=True)
    if not entries or entries[0]["version"] != str(version):
        raise ValueError("Current VERSION has no user-facing changelog entry")
    return entries


def verify_firmware_build(path, *, version, root_version, source_commit, source_tree,
                          test_build=False, variant="normal"):
    path = Path(path)
    if path.stat().st_size > MAX_IMAGE_SIZE:
        raise ValueError("Firmware exceeds inactive partition size")
    record = read_record(Path(str(path) + ".build.json"))
    expected = {"schema": 1, "kind": "sweetmeter-firmware-build", "version": str(version),
                "root_version": str(root_version), "source_commit": source_commit,
                "source_tree": source_tree, "dirty": False, "test_build": test_build,
                "variant": variant}
    if any(record.get(key) != value or type(record.get(key)) is not type(value) for key, value in expected.items()):
        raise ValueError("Firmware build provenance is stale, dirty, or does not match this release")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("source_fingerprint", "")):
        raise ValueError("Missing firmware build source fingerprint")
    project = "Sweetmeter" if not test_build else {
        "normal": "Sweetmeter-test", "health-fail": "Sweetmeter-test-health",
        "reset-before-confirm": "Sweetmeter-test-reset"}[variant]
    image = path.read_bytes()
    if record.get("project_name") != project or image[80:112].split(b"\0", 1)[0] != project.encode():
        raise ValueError("Firmware build variant/project marker mismatch")
    if record.get("image_sha256") != hashlib.sha256(image).hexdigest() or firmware_image_version(image) != Version.parse(version):
        raise ValueError("Firmware bytes/version differ from build-time provenance")
    return record


def verify_companion_package(path, version, *, os_name, arch, source_commit, source_tree):
    """Reuse install-time archive validation before checking bundled release trust."""
    from meter.self_update import inspect_package
    inspect_package(path, os_name=os_name, arch=arch, version=str(version),
                    source_commit=source_commit, source_tree=source_tree)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Companion archive contains duplicate paths")
        regular_names = [name for name in names if not archive.getinfo(name).is_dir()
                         and stat.S_IFMT(archive.getinfo(name).external_attr >> 16) in (0, stat.S_IFREG)]
        version_paths = [name for name in regular_names if name.endswith(("/_internal/VERSION", "/Contents/Frameworks/VERSION", "/Contents/Resources/VERSION"))]
        key_paths = [name for name in regular_names if name.endswith("/meter/assets/keys/release-1.pem")]
        if not version_paths or not key_paths:
            raise ValueError("Companion package lacks bundled VERSION or public trust resource")
        def public_identity(raw):
            return serialization.load_pem_public_key(raw).public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

        expected_public = public_identity((ROOT / "meter/assets/keys/release-1.pem").read_bytes())
        for name in version_paths:
            if archive.getinfo(name).file_size > 20 or archive.read(name).decode("ascii").removesuffix("\n") != str(version):
                raise ValueError("Companion package VERSION differs from release")
        for name in key_paths:
            if archive.getinfo(name).file_size > 4096 or public_identity(archive.read(name)) != expected_public:
                raise ValueError("Companion package signing trust differs from release")


def build_release(*, firmware, companions, minimum_companion, output, key, commit,
                  version, changes, published_at=None):
    """Create new output only; package inputs are copied without executing them."""
    version = Version.parse(version)
    minimum_companion = Version.parse(minimum_companion)
    if version != Version.parse(get_version()):
        raise ValueError("Release version must equal the checked-out root VERSION")
    if minimum_companion > version:
        raise ValueError("Firmware minimum companion cannot exceed release version")
    state = capture_build_state(ROOT)
    if state["dirty"] or state["source_commit"] != commit:
        raise ValueError("Signing requires clean committed source matching release commit")
    firmware = Path(firmware)
    verify_firmware_build(firmware, version=version, root_version=version,
                          source_commit=commit, source_tree=state["source_tree"])
    if firmware.stat().st_size > MAX_IMAGE_SIZE:
        raise ValueError("Firmware exceeds inactive partition size")
    image = firmware.read_bytes()
    if firmware_image_version(image) != version:
        raise ValueError("Compiled ESP app version differs from release VERSION")
    project_name = image[80:112].split(b"\0", 1)[0]
    if project_name != b"Sweetmeter":
        raise ValueError("Only production Sweetmeter app images may become release assets")
    public = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = trusted_keys()[KEY_ID].public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
    if public != expected:
        raise ValueError("Private signing key does not match embedded release trust")
    meta = FirmwareMetadata(version, minimum_companion, len(image), hashlib.sha256(image).digest())
    envelope = sign_envelope(meta, key)
    image_name = f"Sweetmeter-{version}-firmware.bin"
    metadata_name = f"Sweetmeter-{version}-firmware.ota"
    base_url = f"https://github.com/luvxinc/Sweetmeter/releases/download/{version}/"
    artifacts = [{"kind": "firmware", "version": str(version), "asset": image_name,
                  "url": base_url + image_name, "size": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                  "board": BOARD_ID, "protocol": PROTOCOL, "minimum_companion": str(minimum_companion),
                  "metadata_asset": metadata_name, "metadata_url": base_url + metadata_name,
                  "metadata_size": len(envelope), "metadata_sha256": hashlib.sha256(envelope).hexdigest()}]
    copies = []
    for os_name, arch, source in companions:
        source = Path(source)
        verify_companion_package(source, version, os_name=os_name, arch=arch,
                                 source_commit=commit, source_tree=state["source_tree"])
        name = f"Sweetmeter-{version}-{os_name}-{arch}.zip"
        size = source.stat().st_size
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        artifacts.append({"kind": "companion", "version": str(version), "asset": name,
                          "url": base_url + name, "size": size, "sha256": digest.hexdigest(),
                          "os": os_name, "arch": arch})
        copies.append((source, name))
    manifest = {"schema": 1, "product": "Sweetmeter", "channel": "stable", "version": str(version),
                "published_at": published_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commit": commit, "key_id": KEY_ID, "changes": changes, "artifacts": artifacts}
    raw = manifest_bytes(manifest)
    signature = key.sign(raw, ec.ECDSA(hashes.SHA256()))
    verify_manifest(raw, signature)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / image_name).write_bytes(image)
    (output / metadata_name).write_bytes(envelope)
    for source, name in copies:
        shutil.copyfile(source, output / name)
    for artifact in artifacts:
        verify_artifact(output / artifact["asset"], artifact)
    verify_artifact(output / metadata_name, {"size": len(envelope), "sha256": hashlib.sha256(envelope).hexdigest()})
    (output / "manifest.json").write_bytes(raw)
    (output / "manifest.json.sig").write_bytes(signature)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", required=True, type=Path)
    parser.add_argument("--companion", action="append", default=[], help="os:arch:path (repeat for each native ZIP)")
    parser.add_argument("--minimum-companion", required=True)
    parser.add_argument("--output", required=True, type=Path, help="New output directory; existing files are never replaced")
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--published-at")
    args = parser.parse_args()
    try:
        if any(name.startswith("SWEETMETER_TEST_") for name in os.environ):
            raise ValueError("Production signing refuses SWEETMETER_TEST_* environment variables")
        actual_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if args.commit != actual_commit:
            raise ValueError("Release commit must equal the checked-out source commit")
        version = get_version()
        committed_version = subprocess.check_output(["git", "show", f"{args.commit}:VERSION"], cwd=ROOT, text=True).strip()
        if version != committed_version:
            raise ValueError("Uncommitted VERSION cannot be published")
        subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "meter", "firmware", "scripts", "VERSION", "CHANGELOG.md"], cwd=ROOT, check=True)
        companions = []
        for value in args.companion:
            parts = value.split(":", 2)
            if len(parts) != 3:
                raise ValueError("Companion argument must be os:arch:path")
            companions.append(tuple(parts))
        result = build_release(firmware=args.firmware, companions=companions,
                               minimum_companion=args.minimum_companion, output=args.output,
                               key=load_private_key(args.key_file), commit=args.commit, version=version,
                               changes=changelog_history(ROOT / "CHANGELOG.md", version), published_at=args.published_at)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release signing failed: {exc}\n")
    print(f"Verified signed release {result['version']}: {len(result['artifacts'])} artifacts")


if __name__ == "__main__":
    main()
