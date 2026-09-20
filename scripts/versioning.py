#!/usr/bin/env python3
"""Sweetmeter's append-only, per-commit calendar version policy (stdlib only)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


BOOTSTRAP_SHA = "a2a4898eb5a3d1f2e2d9043cba82e59b33b8970b"
VERSION_PATH = "VERSION"
CHANGELOG_PATH = "CHANGELOG.md"
NOTES_PATH = ".changes/next.md"
METADATA_PATHS = (VERSION_PATH, CHANGELOG_PATH, NOTES_PATH)
CHANGELOG_HEADER = (
    "# Changelog\n\n"
    "User-facing changes for every accepted commit, in chronological order.\n"
    "Versions use `YYYY.M.N`, with the month determined in UTC.\n"
)
MAX_SEQUENCE = (1 << 32) - 1
VERSION_RE = re.compile(r"([1-9][0-9]{3})\.([1-9]|1[0-2])\.([1-9][0-9]{0,9})\n\Z")
ENTRY_RE = re.compile(r"\n## \[([^\]\n]+)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2})\n\n(.+)\n\Z", re.S)
SHA_RE = re.compile(r"[0-9a-fA-F]{40,64}\Z")
PLACEHOLDERS = {
    "update", "updates", "updated", "change", "changes", "changelog", "test",
    "test note", "fix", "fix bug", "fix bugs", "bug fix", "bug fixes", "wip",
    "todo", "tbd", "placeholder", "lorem ipsum", "n/a", "none", "misc",
    "更新", "修复", "修改", "待补充", "占位", "测试", "更新日志",
}


class PolicyError(Exception):
    """A human-readable policy failure; never print credentials or Git output."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise PolicyError(f"Git command failed: git {args[0]}; check repository/history availability.")
    return result


def repository() -> Path:
    result = git(Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(result.stdout).strip())


def resolve(repo: Path, ref: str) -> str:
    result = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", check=False)
    if result.returncode:
        raise PolicyError(f"Cannot resolve commit {ref!r}; fetch complete history first.")
    return result.stdout.decode("ascii").strip()


def blob(repo: Path, ref: str, path: str) -> bytes | None:
    # ref is a resolved SHA or the empty string for the index; paths are constants.
    result = git(repo, "show", f"{ref}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None


def utf8(data: bytes | None, label: str) -> str:
    if data is None:
        raise PolicyError(f"Missing {label}.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{label} must be UTF-8.") from exc


def parse_version(data: bytes | str | None) -> tuple[int, int, int]:
    text = utf8(data, VERSION_PATH) if not isinstance(data, str) else data
    match = VERSION_RE.fullmatch(text)
    if not match:
        raise PolicyError("VERSION must be YYYY.M.N plus one newline; M and N have no leading zeroes.")
    version = tuple(map(int, match.groups()))
    if version[2] > MAX_SEQUENCE:
        raise PolicyError(f"VERSION sequence must fit the firmware's unsigned 32-bit field (1..{MAX_SEQUENCE}).")
    return version


def version_text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def next_version(previous: tuple[int, int, int] | None, now: datetime) -> tuple[int, int, int]:
    now = now.astimezone(timezone.utc)
    month = (now.year, now.month)
    if previous is None:
        return (*month, 1)
    if month < previous[:2]:
        raise PolicyError("Commit month goes backwards; check the UTC clock and commit date.")
    if month == previous[:2] and previous[2] >= MAX_SEQUENCE:
        raise PolicyError("This month's unsigned 32-bit version sequence is exhausted; do not wrap or reuse a version.")
    return (*month, previous[2] + 1 if month == previous[:2] else 1)


def read_notes(text: str) -> list[str]:
    notes = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith("- ") or line != line.rstrip():
            raise PolicyError("Each change note must be one '- User-visible change' line, without trailing spaces.")
        note = line[2:]
        simplified = note.strip().strip(" .!?:;。！：；…-*[]").casefold()
        letters = sum(c.isalnum() for c in note)
        non_ascii = sum(c.isalnum() and ord(c) > 127 for c in note)
        if (
            simplified in PLACEHOLDERS or re.match(r"^(todo|tbd|wip|placeholder)\b", simplified)
            or re.search(r"\[(todo|tbd|placeholder)\]", simplified)
            or (letters < 12 and non_ascii < 4)
            or any(ord(c) < 32 or ord(c) == 127 for c in note)
        ):
            raise PolicyError("Write a specific user-facing change; placeholders and generic 'update/fix' notes are refused.")
        notes.append(note)
    if not notes:
        raise PolicyError("No staged change notes. Run: python3 scripts/versioning.py note 'What changed for users'.")
    if len(notes) != len(set(notes)):
        raise PolicyError("Duplicate change notes are not allowed.")
    return notes


def entry(version: tuple[int, int, int], now: datetime, notes: list[str]) -> str:
    date = now.astimezone(timezone.utc).date().isoformat()
    return f"\n## [{version_text(version)}] - {date}\n\n" + "".join(f"- {note}\n" for note in notes)


def validate_metadata(
    current: dict[str, bytes | None], parent: dict[str, bytes | None],
    now: datetime, *, first: bool = False,
) -> tuple[int, int, int]:
    previous = None if first else parse_version(parent[VERSION_PATH])
    expected = next_version(previous, now)
    actual = parse_version(current[VERSION_PATH])
    if actual != expected:
        raise PolicyError(f"Expected VERSION {version_text(expected)}, got {version_text(actual)}.")
    prefix = CHANGELOG_HEADER if first else utf8(parent[CHANGELOG_PATH], "parent CHANGELOG.md")
    changelog = utf8(current[CHANGELOG_PATH], CHANGELOG_PATH)
    if not changelog.startswith(prefix):
        raise PolicyError("CHANGELOG.md is append-only; existing entries cannot be edited or removed.")
    suffix = changelog[len(prefix):]
    match = ENTRY_RE.fullmatch(suffix)
    if not match:
        raise PolicyError("Append exactly one changelog entry for this commit.")
    if match[1] != version_text(actual) or match[2] != now.astimezone(timezone.utc).date().isoformat():
        raise PolicyError("Changelog version/date must match VERSION and the commit's UTC date.")
    read_notes(match[3] + "\n")
    if current[NOTES_PATH] != b"":
        raise PolicyError("Committed .changes/next.md must be empty; the commit hook consumes pending notes.")
    return actual


def get_metadata(repo: Path, ref: str) -> dict[str, bytes | None]:
    return {path: blob(repo, ref, path) for path in METADATA_PATHS}


def checked_worktree(repo: Path) -> dict[str, bytes | None]:
    staged = get_metadata(repo, "")
    for name, data in staged.items():
        path = repo / name
        if path.is_symlink():
            raise PolicyError(f"{name} must be a regular file, not a symlink.")
        local = path.read_bytes() if path.exists() else None
        if local != data:
            raise PolicyError(f"{name} has unstaged changes; stage it explicitly before committing. No data was overwritten.")
        mode = git(repo, "ls-files", "--stage", "--", name).stdout
        if mode and not mode.startswith(b"100644 "):
            raise PolicyError(f"{name} must be a non-executable regular file in the index.")
    return staged


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(repo: Path, now: datetime | None = None, *, bootstrap: str = BOOTSTRAP_SHA) -> str:
    real_clock = now is None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if real_clock:
        # Git honors GIT_COMMITTER_DATE. Catch intentional/stale overrides before
        # preparing files instead of waiting for post-commit or CI rejection.
        identity = git(repo, "var", "GIT_COMMITTER_IDENT").stdout.decode("utf-8")
        match = re.search(r"> ([0-9]+) [+-][0-9]{4}\s*$", identity)
        if not match:
            raise PolicyError("Cannot determine Git's effective committer timestamp.")
        commit_date = datetime.fromtimestamp(int(match[1]), timezone.utc).date()
        if commit_date != now.date():
            raise PolicyError("Commit UTC date must be today's UTC date; remove stale GIT_COMMITTER_DATE overrides.")
    head = resolve(repo, "HEAD")
    # Refuse to build another version on a broken or rewritten history.
    check_history(repo, head, bootstrap=bootstrap)
    staged = checked_worktree(repo)
    parent = get_metadata(repo, head)
    first = head == bootstrap
    previous = None if first else parse_version(parent[VERSION_PATH])
    expected = next_version(previous, now)
    pending = utf8(staged[NOTES_PATH], NOTES_PATH) if staged[NOTES_PATH] is not None else ""

    if not pending.strip():
        # A failed commit can leave the already prepared index intact. Validate it
        # against HEAD; never increment twice for a retry.
        try:
            validate_metadata(staged, parent, now, first=first)
        except PolicyError as exc:
            raise PolicyError("Missing staged notes or invalid prepared metadata: " + str(exc)) from exc
        return f"Already prepared {version_text(expected)}; keeping the same version."

    notes = read_notes(pending)
    # Fresh notes may coexist with a previously prepared entry (e.g. a commit
    # failed and its author adds another meaningful note). Rebuild only that new
    # entry; never allow arbitrary manual changes to historic metadata.
    parent_version = parent[VERSION_PATH]
    parent_log = parent[CHANGELOG_PATH]
    untouched = staged[VERSION_PATH] == parent_version and staged[CHANGELOG_PATH] == parent_log
    if first:
        untouched = staged[VERSION_PATH] is None and staged[CHANGELOG_PATH] is None
    if not untouched:
        candidate = dict(staged)
        candidate[NOTES_PATH] = b""
        validate_metadata(candidate, parent, now, first=first)
        old_prefix = CHANGELOG_HEADER if first else utf8(parent_log, CHANGELOG_PATH)
        old_entry = ENTRY_RE.fullmatch(utf8(staged[CHANGELOG_PATH], CHANGELOG_PATH)[len(old_prefix):])
        assert old_entry is not None
        notes = list(dict.fromkeys(read_notes(old_entry[3] + "\n") + notes))

    prefix = CHANGELOG_HEADER if first else utf8(parent_log, CHANGELOG_PATH)
    generated = {
        VERSION_PATH: (version_text(expected) + "\n").encode(),
        CHANGELOG_PATH: (prefix + entry(expected, now, notes)).encode(),
        NOTES_PATH: b"",
    }
    validate_metadata(generated, parent, now, first=first)
    for name, data in generated.items():
        atomic_write(repo / name, data)
    git(repo, "add", "--", *METADATA_PATHS)
    return f"Prepared {version_text(expected)} with {len(notes)} user-facing change note(s)."


def record_note(repo: Path, message: str) -> None:
    if "\n" in message or "\r" in message:
        raise PolicyError("Pass one note per command; embedded line breaks are not allowed.")
    read_notes(f"- {message}\n")
    path = repo / NOTES_PATH
    if path.is_symlink():
        raise PolicyError("Pending notes cannot be a symlink.")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    notes = read_notes(current) if current.strip() else []
    if message in notes:
        raise PolicyError("That change note is already pending.")
    notes.append(message)
    atomic_write(path, "".join(f"- {note}\n" for note in notes).encode())
    git(repo, "add", "--", NOTES_PATH)


def check_history(
    repo: Path, head: str = "HEAD", *, base: str | None = None,
    bootstrap: str = BOOTSTRAP_SHA,
) -> int:
    head = resolve(repo, head)
    bootstrap = resolve(repo, bootstrap)
    if git(repo, "merge-base", "--is-ancestor", bootstrap, head, check=False).returncode:
        raise PolicyError("History must descend from the original Sweetmeter bootstrap commit.")
    if base is not None:
        resolved_base = resolve(repo, base)
        if git(repo, "merge-base", "--is-ancestor", resolved_base, head, check=False).returncode:
            raise PolicyError("History must be a fast-forward of the base; rewriting accepted commits is forbidden.")
    # Validate the complete chain, including bad intermediate commits even if a
    # later commit repairs the files. --base is an ancestry constraint, not a
    # shortcut that skips earlier policy violations.
    lines = git(repo, "rev-list", "--reverse", "--topo-order", f"{bootstrap}..{head}").stdout.splitlines()
    for raw in lines:
        commit = raw.decode("ascii")
        details = git(repo, "show", "-s", "--format=%P%n%ct", commit).stdout.decode("ascii").splitlines()
        parents = details[0].split()
        if len(parents) != 1:
            raise PolicyError(f"{commit[:12]}: merge/root commits are forbidden after bootstrap; use a linear fast-forward history.")
        parent = parents[0]
        when = datetime.fromtimestamp(int(details[1]), timezone.utc)
        try:
            validate_metadata(get_metadata(repo, commit), get_metadata(repo, parent), when, first=parent == bootstrap)
            for name in METADATA_PATHS:
                tree = git(repo, "ls-tree", commit, "--", name).stdout
                if not tree.startswith(b"100644 blob "):
                    raise PolicyError(f"{name} must be a regular, non-executable file.")
        except PolicyError as exc:
            raise PolicyError(f"{commit[:12]}: {exc}") from exc
    return len(lines)


def pre_push(repo: Path, lines: str, *, bootstrap: str = BOOTSTRAP_SHA) -> int:
    checked = set()
    for line in lines.splitlines():
        values = line.split()
        if len(values) != 4:
            raise PolicyError("Malformed pre-push ref input.")
        local_ref, local_sha, remote_ref, remote_sha = values
        if not SHA_RE.fullmatch(local_sha) or not SHA_RE.fullmatch(remote_sha):
            raise PolicyError("Malformed commit ID in pre-push input.")
        if set(local_sha) == {"0"}:
            if remote_ref in {"refs/heads/main", "refs/heads/master"}:
                raise PolicyError("Deleting the main branch is forbidden.")
            continue
        local_commit = resolve(repo, local_sha)
        if set(remote_sha) != {"0"}:
            remote_commit = resolve(repo, remote_sha)
            if remote_ref.startswith("refs/tags/") and local_sha != remote_sha:
                raise PolicyError("Published tags cannot be rewritten.")
            if git(repo, "merge-base", "--is-ancestor", remote_commit, local_commit, check=False).returncode:
                raise PolicyError("Non-fast-forward push refused; accepted history is append-only.")
        if local_commit not in checked:
            check_history(repo, local_commit, bootstrap=bootstrap)
            checked.add(local_commit)
    return len(checked)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    note = commands.add_parser("note", help="Write and stage one specific user-facing note")
    note.add_argument("message")
    commands.add_parser("prepare", help="Commit hook: consume staged notes and prepare version/changelog")
    history = commands.add_parser("check-history", help="Validate every commit since the original bootstrap")
    history.add_argument("--head", default="HEAD")
    history.add_argument("--base", help="Additionally require head to fast-forward this commit")
    push = commands.add_parser("pre-push", help="Validate outgoing ref lines from standard input")
    push.add_argument("remote", nargs="?")
    push.add_argument("url", nargs="?")
    commands.add_parser("post-commit", help="Report invalid local commits, including accidental amend")
    args = parser.parse_args(argv)
    try:
        repo = repository()
        if args.command == "note":
            record_note(repo, args.message)
            print(f"Staged a user-facing note in {NOTES_PATH}.")
        elif args.command == "prepare":
            print(prepare(repo))
        elif args.command == "pre-push":
            print(f"Version policy: checked {pre_push(repo, sys.stdin.read())} outgoing commit tip(s).")
        else:
            count = check_history(repo, getattr(args, "head", "HEAD"), base=getattr(args, "base", None))
            print(f"Version policy: {count} commit(s) valid after bootstrap.")
        return 0
    except (PolicyError, OSError, UnicodeError, ValueError) as exc:
        print(f"Sweetmeter version policy: {exc}", file=sys.stderr)
        if args.command == "post-commit":
            print("This commit already exists locally but cannot be pushed. Do not rewrite published history; see CONTRIBUTING.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
