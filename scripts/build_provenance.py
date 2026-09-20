"""Small local build provenance records; not remote attestation.

Capture before and after a build to reject accidental source changes. A clean
Git commit/tree identifies normalized source across native operating systems;
the worktree fingerprint detects changes during this local build only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import stat
import subprocess


def capture_build_state(root):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)
    commit = git("rev-parse", "HEAD").decode("ascii").strip()
    tree = git("rev-parse", "HEAD^{tree}").decode("ascii").strip()
    dirty = bool(git("status", "--porcelain", "--untracked-files=all").strip())
    names = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""})
    digest = hashlib.sha256()
    for name in names:
        path = root / os.fsdecode(name)
        digest.update(len(name).to_bytes(4, "little") + name)
        try:
            info = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"link\0" + os.fsencode(os.readlink(path)) + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"file\0" + str(info.st_mode & 0o111).encode() + b"\0")
            file_hash = hashlib.sha256()
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    file_hash.update(block)
            digest.update(file_hash.digest())
        else:
            raise ValueError("Unsupported non-regular source tree entry")
    return {"source_commit": commit, "source_tree": tree, "dirty": dirty,
            "source_fingerprint": digest.hexdigest()}


def require_unchanged_build_state(before, root):
    if capture_build_state(root) != before:
        raise ValueError("Source changed during build; rebuild from one consistent source tree")


def write_json_atomic(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_record(path, *, maximum=16384):
    raw = Path(path).read_bytes()
    if not raw or len(raw) > maximum:
        raise ValueError("Invalid build provenance size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate build provenance key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Build provenance must be an object")
    return value
