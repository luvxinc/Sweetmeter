"""Exercise version policy against real, isolated Git repositories."""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sweetmeter_versioning", ROOT / "scripts/versioning.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
SEPTEMBER = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sweetmeter version tests ")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Version Policy Test")
        self.run_git("config", "user.email", "version-test@example.invalid")
        self.run_git("config", "core.autocrlf", "false")
        self.run_git("config", "commit.gpgsign", "false")
        self.run_git("config", "tag.gpgsign", "false")
        self.save("README.md", "Synthetic test repository.\n")
        self.run_git("add", "README.md")
        self.commit("Bootstrap", SEPTEMBER)
        self.bootstrap = self.head()

    def run_git(self, *args, date=None, success=True):
        env = os.environ.copy()
        # Do not inherit a hook's alternate index when tests run from a hook.
        for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
            env.pop(key, None)
        if date:
            stamp = date.strftime("%Y-%m-%dT%H:%M:%S%z")
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        result = subprocess.run(["git", "-C", str(self.repo), *args], env=env, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def save(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def head(self):
        return self.run_git("rev-parse", "HEAD").stdout.decode().strip()

    def commit(self, message="Implement a visible change", date=SEPTEMBER, *extra):
        return self.run_git("commit", *extra, "-m", message, date=date)

    def note(self, message="Show reset countdowns beside the quota meters."):
        policy.record_note(self.repo, message)

    def prepare(self, date=SEPTEMBER):
        return policy.prepare(self.repo, date, bootstrap=self.bootstrap)

    def valid_commit(self, date=SEPTEMBER, message="Show reset countdowns beside the quota meters."):
        self.note(message)
        self.prepare(date)
        self.commit(date=date)
        return self.head()

    def validate(self, head="HEAD", **kwargs):
        return policy.check_history(self.repo, head, bootstrap=self.bootstrap, **kwargs)

    def install_hooks(self):
        shutil.copytree(ROOT / ".githooks", self.repo / ".githooks")
        (self.repo / "scripts").mkdir(exist_ok=True)
        script = (ROOT / "scripts/versioning.py").read_text()
        self.save("scripts/versioning.py", script.replace(policy.BOOTSTRAP_SHA, self.bootstrap))
        shutil.copyfile(ROOT / "scripts/install_hooks.py", self.repo / "scripts/install_hooks.py")
        result = subprocess.run([sys.executable, str(self.repo / "scripts/install_hooks.py")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.repo / ".githooks"

    def test_bootstrap_and_exact_same_month_increment(self):
        self.assertEqual(self.validate(), 0)
        self.valid_commit()
        self.assertEqual((self.repo / "VERSION").read_text(), "2026.9.1\n")
        original = (self.repo / "CHANGELOG.md").read_text()
        self.valid_commit(message="Keep the previous quota visible during a reconnect.")
        self.assertEqual((self.repo / "VERSION").read_text(), "2026.9.2\n")
        self.assertTrue((self.repo / "CHANGELOG.md").read_text().startswith(original))
        self.assertEqual(self.validate(), 2)

    def test_month_and_year_rollover_reset_sequence(self):
        self.valid_commit()
        self.valid_commit(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual((self.repo / "VERSION").read_text(), "2026.10.1\n")
        self.valid_commit(datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual((self.repo / "VERSION").read_text(), "2027.1.1\n")
        self.assertEqual(self.validate(), 3)

    def test_utc_month_uses_instant_not_local_month(self):
        offset_time = datetime.fromisoformat("2026-10-01T00:15:00+09:00")
        self.valid_commit(offset_time)
        self.assertEqual((self.repo / "VERSION").read_text(), "2026.9.1\n")
        self.assertIn("2026-09-30", (self.repo / "CHANGELOG.md").read_text())
        self.assertEqual(self.validate(), 1)

    def test_backwards_month_and_wrong_commit_clock_are_rejected(self):
        self.valid_commit()
        self.note()
        with self.assertRaisesRegex(policy.PolicyError, "backwards"):
            self.prepare(datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.prepare()
        self.commit(date=datetime(2026, 10, 1, tzinfo=timezone.utc))
        with self.assertRaisesRegex(policy.PolicyError, "Expected VERSION 2026.10.1"):
            self.validate()

    def test_missing_unstaged_and_placeholder_notes_fail_without_writes(self):
        with self.assertRaisesRegex(policy.PolicyError, "Missing staged"):
            self.prepare()
        self.save(policy.NOTES_PATH, "- Describe the visible Bluetooth reconnection improvement.\n")
        with self.assertRaisesRegex(policy.PolicyError, "unstaged changes"):
            self.prepare()
        self.assertFalse((self.repo / "VERSION").exists())
        for message in ("TODO", "fix", "update", "bug fixes", "placeholder details", "待补充", "..."):
            with self.subTest(message=message), self.assertRaises(policy.PolicyError):
                policy.record_note(self.repo, message)

    def test_non_ascii_notes_are_preserved(self):
        self.note("蓝牙断线重连时继续保留上次的额度显示。")
        self.prepare()
        self.commit()
        self.assertIn("蓝牙断线重连", (self.repo / "CHANGELOG.md").read_text())
        self.assertEqual(self.validate(), 1)

    def test_duplicate_and_multiline_notes_fail(self):
        self.note()
        with self.assertRaisesRegex(policy.PolicyError, "already pending"):
            self.note()
        with self.assertRaisesRegex(policy.PolicyError, "line breaks"):
            self.note("One meaningful change\n- Another hidden note")
        self.save(policy.NOTES_PATH, "- Improve Bluetooth reliability.\n- Improve Bluetooth reliability.\n")
        self.run_git("add", policy.NOTES_PATH)
        with self.assertRaisesRegex(policy.PolicyError, "Duplicate"):
            self.prepare()

    def test_retry_is_idempotent_and_new_notes_extend_same_entry(self):
        self.note()
        self.prepare()
        before = policy.get_metadata(self.repo, "")
        self.assertIn("Already prepared", self.prepare())
        self.assertEqual(before, policy.get_metadata(self.repo, ""))
        self.note("Display the actual subscription below the provider name.")
        self.prepare()
        log = (self.repo / "CHANGELOG.md").read_text()
        self.assertEqual(log.count("## ["), 1)
        self.assertIn("reset countdowns", log)
        self.assertIn("actual subscription", log)
        self.commit()
        with self.assertRaisesRegex(policy.PolicyError, "Missing staged"):
            self.prepare()

    def test_partial_staging_preserves_unstaged_source_and_rejects_unstaged_metadata(self):
        self.valid_commit()
        self.save("README.md", "Staged source description.\n")
        self.run_git("add", "README.md")
        self.save("README.md", "Unstaged source description.\n")
        self.note()
        self.prepare()
        self.assertEqual((self.repo / "README.md").read_text(), "Unstaged source description.\n")
        self.assertEqual(policy.blob(self.repo, "", "README.md"), b"Staged source description.\n")
        self.save("VERSION", "2026.9.999\n")
        before = policy.get_metadata(self.repo, "")
        with self.assertRaisesRegex(policy.PolicyError, "VERSION has unstaged"):
            self.prepare()
        self.assertEqual(before, policy.get_metadata(self.repo, ""))
        self.assertEqual((self.repo / "VERSION").read_text(), "2026.9.999\n")

    def test_unstaged_changelog_or_notes_never_get_overwritten(self):
        self.valid_commit()
        for name in (policy.CHANGELOG_PATH, policy.NOTES_PATH):
            with self.subTest(name=name):
                original = (self.repo / name).read_bytes()
                (self.repo / name).write_bytes(original + b"Unstaged personal draft.\n")
                with self.assertRaisesRegex(policy.PolicyError, "unstaged changes"):
                    self.prepare()
                self.assertIn(b"personal draft", (self.repo / name).read_bytes())
                (self.repo / name).write_bytes(original)

    def test_malformed_version_zeroes_and_missing_newline(self):
        for version in ("2026.09.1\n", "2026.9.01\n", "2026.9.0\n", "2026.13.1\n", "2026.9.1", "2026.9.1\n\n"):
            with self.subTest(version=version), self.assertRaises(policy.PolicyError):
                policy.parse_version(version)

    def test_sequence_fits_firmware_u32_and_never_wraps(self):
        maximum = policy.MAX_SEQUENCE
        self.assertEqual(policy.parse_version(f"2026.9.{maximum}\n"), (2026, 9, maximum))
        self.assertEqual(policy.next_version((2026, 9, maximum - 1), SEPTEMBER), (2026, 9, maximum))
        for sequence in (maximum + 1, 10 ** 20):
            with self.subTest(sequence=sequence), self.assertRaises(policy.PolicyError):
                policy.parse_version(f"2026.9.{sequence}\n")
        with self.assertRaisesRegex(policy.PolicyError, "exhausted"):
            policy.next_version((2026, 9, maximum), SEPTEMBER)
        self.assertEqual(policy.next_version((2026, 9, maximum), datetime(2026, 10, 1, tzinfo=timezone.utc)), (2026, 10, 1))

    def test_skipped_hook_and_bad_middle_commit_are_detected(self):
        first = self.valid_commit()
        self.save("README.md", "Unversioned change.\n")
        self.run_git("add", "README.md")
        self.commit("Skip the hooks")
        bad = self.head()
        with self.assertRaisesRegex(policy.PolicyError, bad[:12]):
            self.validate()
        # Repair tip files without repairing history; the bad middle must remain visible.
        previous = policy.get_metadata(self.repo, first)
        self.save("VERSION", "2026.9.2\n")
        self.save("CHANGELOG.md", previous[policy.CHANGELOG_PATH].decode() + policy.entry((2026, 9, 2), SEPTEMBER, ["Repair the visible quota percentage alignment."]))
        self.run_git("add", "VERSION", "CHANGELOG.md")
        self.commit("Repair only the tip")
        with self.assertRaisesRegex(policy.PolicyError, bad[:12]):
            self.validate()
        self.note()
        with self.assertRaisesRegex(policy.PolicyError, bad[:12]):
            self.prepare()

    def test_append_only_exactly_one_entry_and_matching_date(self):
        first = self.valid_commit()
        parent = policy.get_metadata(self.repo, first)
        next_day = datetime(2026, 9, 21, tzinfo=timezone.utc)
        valid = {
            policy.VERSION_PATH: b"2026.9.2\n",
            policy.CHANGELOG_PATH: parent[policy.CHANGELOG_PATH] + policy.entry((2026, 9, 2), next_day, ["Improve the battery icon when no gauge is connected."]).encode(),
            policy.NOTES_PATH: b"",
        }
        policy.validate_metadata(valid, parent, next_day)
        for name, value, expected in (
            (policy.CHANGELOG_PATH, valid[policy.CHANGELOG_PATH].replace(b"reset countdowns", b"historic edits"), "append-only"),
            (policy.CHANGELOG_PATH, valid[policy.CHANGELOG_PATH] + policy.entry((2026, 9, 3), next_day, ["Unexpected extra entry should not be accepted."]).encode(), "Each change note"),
            (policy.CHANGELOG_PATH, valid[policy.CHANGELOG_PATH].replace(b"2026-09-21", b"2026-09-22"), "version/date"),
            (policy.VERSION_PATH, b"2026.9.3\n", "Expected VERSION"),
            (policy.NOTES_PATH, b"- Unconsumed meaningful change note.\n", "must be empty"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(policy.PolicyError, expected):
                policy.validate_metadata({**valid, name: value}, parent, next_day)

    def test_metadata_executable_mode_is_rejected(self):
        self.note()
        self.prepare()
        self.run_git("update-index", "--chmod=+x", "VERSION")
        with self.assertRaisesRegex(policy.PolicyError, "non-executable"):
            self.prepare()
        self.commit()
        with self.assertRaisesRegex(policy.PolicyError, "non-executable"):
            self.validate()

    def test_merge_commit_is_rejected_even_when_metadata_looks_valid(self):
        main = self.valid_commit()
        self.run_git("checkout", "-b", "feature")
        feature = self.valid_commit(message="Make the Bluetooth picker easier to navigate.")
        self.run_git("checkout", "main")
        self.valid_commit(message="Improve wording in the first-time setup instructions.")
        tree = self.run_git("rev-parse", "HEAD^{tree}").stdout.decode().strip()
        merged = self.run_git("commit-tree", tree, "-p", self.head(), "-p", feature, "-m", "Synthetic merge", date=SEPTEMBER).stdout.decode().strip()
        with self.assertRaisesRegex(policy.PolicyError, "merge/root"):
            self.validate(merged)
        self.assertNotEqual(main, feature)

    def test_amend_replaces_parent_and_fails_history_validation(self):
        self.valid_commit()
        self.note("Explain the Bluetooth firmware update confirmation.")
        self.prepare()
        self.commit("Attempt an amend", SEPTEMBER, "--amend")
        with self.assertRaisesRegex(policy.PolicyError, "Expected VERSION 2026.9.1"):
            self.validate()

    def test_pre_push_checks_every_ref_and_refuses_rewrites(self):
        first = self.valid_commit()
        second = self.valid_commit(message="Increase contrast in the quota progress bar labels.")
        zeros = "0" * 40
        self.assertEqual(policy.pre_push(self.repo, f"refs/heads/main {second} refs/heads/main {first}\n", bootstrap=self.bootstrap), 1)
        self.assertEqual(policy.pre_push(self.repo, f"refs/heads/new {second} refs/heads/new {zeros}\n", bootstrap=self.bootstrap), 1)
        with self.assertRaisesRegex(policy.PolicyError, "Non-fast-forward"):
            policy.pre_push(self.repo, f"refs/heads/main {first} refs/heads/main {second}\n", bootstrap=self.bootstrap)
        with self.assertRaisesRegex(policy.PolicyError, "Deleting"):
            policy.pre_push(self.repo, f"(delete) {zeros} refs/heads/main {second}\n", bootstrap=self.bootstrap)
        self.assertEqual(policy.pre_push(self.repo, f"(delete) {zeros} refs/heads/old {second}\n", bootstrap=self.bootstrap), 0)
        with self.assertRaisesRegex(policy.PolicyError, "tags cannot"):
            policy.pre_push(self.repo, f"refs/tags/v {second} refs/tags/v {first}\n", bootstrap=self.bootstrap)
        self.run_git("checkout", "-b", "bad-tip")
        self.save("README.md", "Bypass commit metadata.\n")
        self.run_git("add", "README.md")
        self.commit()
        with self.assertRaises(policy.PolicyError):
            policy.pre_push(self.repo, f"refs/heads/main {second} refs/heads/main {first}\nrefs/heads/bad {self.head()} refs/heads/bad {zeros}\n", bootstrap=self.bootstrap)

    def test_base_constraint_catches_diverged_branch(self):
        self.valid_commit()
        self.run_git("checkout", "-b", "parallel")
        parallel = self.valid_commit(message="Explain first-time connection steps for Linux users.")
        self.run_git("checkout", "main")
        self.valid_commit(message="Clarify the difference between quota and local tokens.")
        with self.assertRaisesRegex(policy.PolicyError, "fast-forward"):
            self.validate(parallel, base="main")

    def test_unrelated_bootstrap_is_not_an_exception(self):
        self.run_git("checkout", "--orphan", "unrelated")
        self.run_git("rm", "-rf", ".")
        self.save("README.md", "Unrelated new repository.\n")
        self.run_git("add", "README.md")
        self.commit()
        with self.assertRaisesRegex(policy.PolicyError, "original Sweetmeter bootstrap"):
            self.validate()

    def test_installed_hook_blocks_missing_notes_then_prepares_and_retries(self):
        hooks = self.install_hooks()
        self.run_git("add", "scripts", ".githooks")
        now = datetime.now(timezone.utc)
        refused = self.run_git("commit", "-m", "Missing notes", date=now, success=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.head(), self.bootstrap)
        self.note("Install version and changelog checks for contributors.")
        # A later hook failure must not cause a second version increment.
        self.save(".githooks/commit-msg", "#!/bin/sh\nexit 1\n")
        os.chmod(hooks / "commit-msg", 0o755)
        failed = self.run_git("commit", "-m", "Later hook fails", date=now, success=False)
        self.assertNotEqual(failed.returncode, 0)
        prepared = (self.repo / "VERSION").read_text()
        (hooks / "commit-msg").unlink()
        self.commit("Retry succeeds", now)
        self.assertEqual((self.repo / "VERSION").read_text(), prepared)
        self.assertEqual(self.validate(), 1)

    def test_no_verify_bypass_is_detected_by_post_commit_and_pre_push(self):
        self.install_hooks()
        self.run_git("add", "scripts", ".githooks")
        now = datetime.now(timezone.utc)
        self.note("Enable commit validation without extra Python dependencies.")
        self.commit(date=now)
        self.save("README.md", "A change without a changelog.\n")
        self.run_git("add", "README.md")
        result = self.run_git("commit", "--no-verify", "-m", "Skip pre-commit", date=now)
        self.assertIn(b"already exists locally", result.stderr)
        with self.assertRaises(policy.PolicyError):
            policy.pre_push(self.repo, f"refs/heads/main {self.head()} refs/heads/main {'0' * 40}\n", bootstrap=self.bootstrap)

    def test_installed_hook_refuses_stale_committer_date_before_writing(self):
        self.install_hooks()
        self.run_git("add", "scripts", ".githooks")
        self.note("Explain when the next monthly version counter starts.")
        result = self.run_git("commit", "-m", "Backdated commit", date=datetime(2001, 1, 1, tzinfo=timezone.utc), success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"today's UTC date", result.stderr)
        self.assertEqual(self.head(), self.bootstrap)
        self.assertFalse((self.repo / "VERSION").exists())
        self.assertIn("monthly version", (self.repo / policy.NOTES_PATH).read_text())

    def test_hook_rejects_path_only_commit_that_omits_staged_notes(self):
        self.install_hooks()
        self.run_git("add", "scripts", ".githooks")
        now = datetime.now(timezone.utc)
        self.note("Preserve pending release notes when staging selected files.")
        self.commit(date=now)
        self.save("README.md", "A changed source description.\n")
        self.note("Clarify the command used to install the contribution hooks.")
        result = self.run_git("commit", "--only", "README.md", "-m", "Omit notes from temporary index", date=now, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unstaged changes", result.stderr)
        self.assertIn(b"contribution hooks", policy.blob(self.repo, "", policy.NOTES_PATH))
        self.assertEqual(self.validate(), 1)

    def test_real_pre_push_hook_blocks_unversioned_branch_creation(self):
        self.install_hooks()
        self.run_git("add", "scripts", ".githooks")
        now = datetime.now(timezone.utc)
        self.note("Check outgoing history before publishing a new branch.")
        self.commit(date=now)
        remote = self.repo / "remote.git"
        self.run_git("init", "--bare", str(remote))
        self.run_git("remote", "add", "origin", str(remote))
        self.run_git("push", "origin", "main")
        self.save("README.md", "An intentionally invalid unversioned change.\n")
        self.run_git("add", "README.md")
        self.run_git("commit", "--no-verify", "-m", "Omit required metadata", date=now)
        rejected = self.run_git("push", "origin", "HEAD:refs/heads/new-branch", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"Expected VERSION", rejected.stderr)

    def test_note_text_is_data_not_shell_code(self):
        message = "Keep literal `example` and $(touch injected-file) text in documentation."
        self.note(message)
        self.prepare()
        self.assertFalse((self.repo / "injected-file").exists())
        self.assertIn(message, (self.repo / "CHANGELOG.md").read_text())

    def test_base_checker_rejects_head_that_replaces_checker_and_skips_version(self):
        # Reproduce the trusted workflow: keep the base worktree, fetch/read the
        # candidate as Git data, and never import or execute its replacement code.
        trusted_script = (ROOT / "scripts/versioning.py").read_text().replace(policy.BOOTSTRAP_SHA, self.bootstrap)
        self.save("scripts/versioning.py", trusted_script)
        self.run_git("add", "scripts/versioning.py")
        base = self.valid_commit()
        self.save("scripts/versioning.py", "from pathlib import Path\nPath('head-code-ran').touch()\nraise SystemExit(0)\n")
        self.run_git("add", "scripts/versioning.py")
        self.commit("Replace the checker without version metadata")
        candidate = self.head()
        self.run_git("checkout", "--detach", base)
        result = subprocess.run(
            [sys.executable, "scripts/versioning.py", "check-history", "--head", candidate, "--base", base],
            cwd=self.repo, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Expected VERSION", result.stderr)
        self.assertFalse((self.repo / "head-code-ran").exists())


if __name__ == "__main__":
    unittest.main()
