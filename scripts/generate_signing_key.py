#!/usr/bin/env python3
"""Create an external P-256 release key once; export only its public key."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]


def generate(private_path: Path, public_path: Path) -> str:
    private_path, public_path = private_path.absolute(), public_path.absolute()
    if private_path.is_symlink() or private_path.resolve().is_relative_to(ROOT):
        raise ValueError("Private signing key must be outside the repository and not a symlink")
    if public_path.is_symlink():
        raise ValueError("Public key output must not be a symlink")
    if public_path.exists() and not private_path.exists():
        raise ValueError("Public trust already exists; recover its private key instead of generating a replacement")
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private_path.exists():
        if not stat.S_ISREG(private_path.stat().st_mode):
            raise ValueError("Private key must be a regular file")
        if os.name != "nt" and private_path.stat().st_mode & 0o077:
            raise ValueError("Existing private key permissions must be 0600")
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("Existing signing key must be P-256")
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        # O_EXCL prevents both accidental rotation and races replacing an existing key.
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_path.exists() and public_path.read_bytes() != public:
        raise ValueError("Existing public key differs; key rotation must be explicit")
    public_path.parent.mkdir(parents=True, exist_ok=True)
    if not public_path.exists():
        with public_path.open("xb") as handle:
            handle.write(public)
    return hashlib.sha256(public).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--public-key", type=Path, default=ROOT / "meter/assets/keys/release-1.pem")
    args = parser.parse_args()
    try:
        fingerprint = generate(args.private_key, args.public_key)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Key setup failed: {exc}\n")
    print(f"Public key SHA-256: {fingerprint}")
    print("Private key retained outside repository; existing keys were not replaced.")


if __name__ == "__main__":
    main()
