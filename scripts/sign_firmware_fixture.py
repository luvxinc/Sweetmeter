#!/usr/bin/env python3
"""Sign a deliberately marked, local acceptance image; never a public release.

Use a committed, clean source checkout. Every output includes an acceptance-only
sidecar binding source commit/tree, variant and exact image/envelope digests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from meter.protocol import FirmwareMetadata, firmware_image_version, verify_envelope
from meter.version import Version, get_version
from scripts.sign_release import load_private_key, sign_envelope, verify_firmware_build

PROJECTS = {"normal": b"Sweetmeter-test", "health-fail": b"Sweetmeter-test-health",
            "reset-before-confirm": b"Sweetmeter-test-reset"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--variant", choices=PROJECTS, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--minimum-companion", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True)
        if status.strip():
            raise ValueError("Acceptance fixtures require clean committed source; commit reviewed changes first")
        version = Version.parse(args.version)
        root_version = Version.parse(get_version())
        if version <= root_version:
            raise ValueError("Fixture version must be explicitly newer than root VERSION")
        if Version.parse(args.minimum_companion) != root_version:
            raise ValueError("Fixture minimum companion must equal the tested root VERSION")
        image = args.firmware.read_bytes()
        if firmware_image_version(image) != version:
            raise ValueError("Fixture app description version differs from requested version")
        if image[80:112].split(b"\0", 1)[0] != PROJECTS[args.variant]:
            raise ValueError("Fixture variant does not match compiled app description marker")
        source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        source_tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
        build_record = verify_firmware_build(args.firmware, version=version, root_version=root_version,
                                             source_commit=source_commit, source_tree=source_tree,
                                             test_build=True, variant=args.variant)
        metadata = FirmwareMetadata(version, root_version, len(image), hashlib.sha256(image).digest())
        envelope = sign_envelope(metadata, load_private_key(args.key_file))
        verify_envelope(envelope, current_version=root_version, companion_version=root_version)
        source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        source_tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
        sidecar = {"schema": 1, "kind": "sweetmeter-acceptance-only", "variant": args.variant,
                   "version": str(version), "root_version": str(root_version),
                   "source_commit": source_commit, "source_tree": source_tree,
                   "build_provenance": build_record,
                   "image_sha256": hashlib.sha256(image).hexdigest(),
                   "metadata_sha256": hashlib.sha256(envelope).hexdigest()}
        args.output.mkdir(parents=True, exist_ok=False)
        stem = f"acceptance-{args.variant}-{version}"
        (args.output / f"{stem}.bin").write_bytes(image)
        (args.output / f"{stem}.ota").write_bytes(envelope)
        (args.output / f"{stem}.acceptance.json").write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Acceptance signing failed: {exc}\n")
    print(f"Local acceptance fixture ready: {args.variant} {version}; do not publish these assets.")


if __name__ == "__main__":
    main()
