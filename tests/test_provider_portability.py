"""Portable provider tests use synthetic credentials and mocked subprocesses."""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from meter import providers
from meter.tokens import TokenIndex, log_roots


class SyntheticHome(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="sweetmeter providers "))
        self.home = Path(folder)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))

    def credentials(self, root, token="synthetic-login", **metadata):
        root.mkdir(parents=True, exist_ok=True)
        value = {"accessToken": token, "expiresAt": 4102444800000, **metadata}
        (root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": value}))
        return value

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path


class CredentialPortabilityTests(SyntheticHome):
    def test_native_windows_and_linux_only_read_their_profile_file(self):
        expected = self.credentials(self.home / ".claude", subscriptionType="pro")
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(providers.sys, "platform", platform), \
                    patch.object(providers.subprocess, "run") as run:
                self.assertEqual(providers.claude_credentials(), expected)
                run.assert_not_called()

    def test_custom_profile_never_falls_back_to_default_credentials(self):
        self.credentials(self.home / ".claude")
        custom = self.home / "separate profile"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}), \
                patch.object(providers.sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "selected profile"):
                providers.claude_credentials()
            expected = self.credentials(custom, token="custom-synthetic-login")
            self.assertEqual(providers.claude_credentials(), expected)

    def test_keychain_service_has_exact_nfc_string_hash(self):
        raw = "~/Cafe\u0301/profile/../profile"
        normalized = "~/Café/profile/../profile"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": raw}):
            root, service = providers.claude_credential_store()
            self.assertEqual(root, Path(normalized))
            expected = hashlib.sha256(normalized.encode()).hexdigest()[:8]
            self.assertEqual(service, "Claude Code-credentials-" + expected)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}):
            self.assertNotEqual(providers.claude_credential_store()[1], "Claude Code-credentials")

    def test_secure_storage_override_and_empty_string_pin(self):
        config = self.home / "config"
        secrets = self.home / "credentials"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config),
                                    "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(secrets)}):
            self.assertEqual(providers.claude_credential_store()[0], secrets)
            self.assertEqual(providers.claude_config_dir(), config)
            with patch.dict(os.environ, {"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}):
                self.assertEqual(providers.claude_credential_store(),
                                 (self.home / ".claude", "Claude Code-credentials"))
                self.assertEqual(providers.claude_config_dir(), config)

    def test_keychain_is_authoritative_and_only_selected_service_is_read(self):
        custom = self.home / "selected profile"
        self.credentials(custom, token="stale-file-login", expiresAt=9999999999999)
        keychain = {"accessToken": "current-keychain-login", "expiresAt": 4102444800000,
                    "subscriptionType": "max"}
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}), \
                patch.object(providers.sys, "platform", "darwin"), \
                patch.object(providers.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({"claudeAiOauth": keychain}).encode())) as run:
            self.assertEqual(providers.claude_credentials(), keychain)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][3], providers.claude_credential_store()[1])
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_keychain_failure_uses_matching_file_without_exposing_process_output(self):
        expected = self.credentials(self.home / ".claude")
        for error in (OSError("synthetic sensitive diagnostic"), subprocess.TimeoutExpired("security", 20)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(providers.sys, "platform", "darwin"), \
                    patch.object(providers.subprocess, "run", side_effect=error):
                self.assertEqual(providers.claude_credentials(), expected)

    def test_expired_keychain_does_not_select_a_different_file_login(self):
        self.credentials(self.home / ".claude")
        with patch.object(providers.sys, "platform", "darwin"), \
                patch.object(providers.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=b'{"claudeAiOauth":{"accessToken":"expired-secret","expiresAt":1}}')):
            with self.assertRaisesRegex(RuntimeError, "expired") as error:
                providers.claude_credentials()
            self.assertNotIn("expired-secret", str(error.exception))

    def test_explicit_token_does_not_borrow_another_accounts_subscription(self):
        self.credentials(self.home / ".claude", subscriptionType="max")
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "explicit-synthetic-login"}), \
                patch.object(providers.subprocess, "run") as run:
            credentials = providers.claude_credentials()
            self.assertEqual(credentials, {"accessToken": "explicit-synthetic-login"})
            self.assertEqual(providers.claude_subscription(credentials), "--")
            run.assert_not_called()

    def test_malformed_credentials_and_expiry_fail_without_echoing_tokens(self):
        root = self.home / ".claude"
        root.mkdir()
        for raw in ("[]", "null", '{"claudeAiOauth":[]}', '{"claudeAiOauth":{"accessToken":123}}'):
            (root / ".credentials.json").write_text(raw)
            with self.subTest(raw=raw), patch.object(providers.sys, "platform", "linux"), \
                    self.assertRaisesRegex(RuntimeError, "login required"):
                providers.claude_credentials()
        for expiry in ("synthetic-sensitive-value", True, float("nan")):
            self.credentials(root, expiresAt=expiry)
            with patch.object(providers.sys, "platform", "linux"), \
                    self.assertRaisesRegex(RuntimeError, "metadata is invalid") as error:
                providers.claude_credentials()
            self.assertNotIn("synthetic-sensitive-value", str(error.exception))


class CodexLauncherTests(SyntheticHome):
    def test_explicit_path_with_shell_metacharacters_is_one_argv_entry(self):
        executable = self.touch(self.home / "space & $(example)" / "codex.exe")
        with patch.object(providers.sys, "platform", "win32"), patch.object(providers.shutil, "which", return_value=None):
            self.assertEqual(providers.codex_command(executable), [str(executable), "app-server"])

    def test_missing_explicit_path_fails_instead_of_using_a_different_installation(self):
        self.touch(self.home / ".local/bin/codex")
        with patch.dict(os.environ, {"SWEETMETER_CODEX_PATH": str(self.home / "missing codex")}), \
                patch.object(providers.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Configured Codex"):
                providers.codex_command()

    def test_path_and_per_user_native_installations(self):
        executable = self.touch(self.home / ".local/bin/codex")
        with patch.object(providers.shutil, "which", return_value=None), \
                patch.object(providers.sys, "platform", "linux"):
            self.assertEqual(providers.codex_command(), [str(executable), "app-server"])
        with patch.object(providers.shutil, "which", return_value=str(executable)):
            self.assertEqual(providers.codex_command(), [str(executable), "app-server"])

    def test_windows_npm_global_shim_prefers_native_optional_dependency(self):
        prefix = self.home / "AppData/Roaming/npm"
        shim = self.touch(prefix / "codex.cmd")
        self.touch(prefix / "node_modules/@openai/codex/bin/codex.js")
        native = self.touch(prefix / "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
        with patch.object(providers.sys, "platform", "win32"), \
                patch.object(providers.platform, "machine", return_value="AMD64"), \
                patch.object(providers.shutil, "which", return_value=None), \
                patch.dict(os.environ, {"APPDATA": str(self.home / "AppData/Roaming")}):
            self.assertEqual(providers.codex_command(), [str(native), "app-server"])
            self.assertEqual(providers.codex_command(shim), [str(native), "app-server"])

    def test_windows_npm_local_shim_resolves_node_and_script_without_shell(self):
        modules = self.home / "project with & characters/node_modules"
        shim = self.touch(modules / ".bin/codex.cmd")
        script = self.touch(modules / "@openai/codex/bin/codex.js")
        node = self.touch(modules / ".bin/node.exe")
        with patch.object(providers.sys, "platform", "win32"), \
                patch.object(providers.shutil, "which", return_value=None):
            self.assertEqual(providers.codex_command(shim), [str(node), str(script), "app-server"])

    def test_unknown_batch_script_is_never_passed_to_cmd(self):
        shim = self.touch(self.home / "arbitrary.cmd")
        with patch.object(providers.sys, "platform", "win32"), \
                patch.object(providers.shutil, "which", return_value=None), \
                patch.object(providers.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "launcher is unsupported"):
                providers.fetch_codex(shim)
            popen.assert_not_called()


class ReadableAfterClose(io.StringIO):
    def close(self):
        self.closed_value = self.getvalue()
        super().close()


class CodexRpcTests(SyntheticHome):
    def process(self, messages):
        process = Mock()
        process.stdin = ReadableAfterClose()
        process.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        process.poll.return_value = None
        process.pid = 12345
        return process

    def test_dynamic_plan_falls_back_to_account_read_and_drops_identity_fields(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"rateLimits": {"primary": {"windowDurationMins": 10080,
                                                                         "usedPercent": 17}}}},
            {"id": 3, "result": {"account": {"type": "chatgpt", "planType": "plus",
                                               "email": "synthetic@example.invalid"}}},
        ])
        with patch.object(providers, "codex_command", return_value=["codex", "app-server"]), \
                patch.object(providers, "get_version", return_value="2026.9.1"), \
                patch.object(providers.sys, "platform", "linux"), \
                patch.object(providers.subprocess, "Popen", return_value=process) as popen:
            rows = providers.fetch_codex()
            self.assertEqual(rows[0]["subscription_label"], "PLUS")
            self.assertEqual(rows[0]["used"], 17)
            self.assertNotIn("email", json.dumps(rows))
            self.assertFalse(popen.call_args.kwargs["shell"])
            sent = [json.loads(line) for line in process.stdin.closed_value.splitlines()]
            self.assertEqual(sent[0]["params"]["clientInfo"]["version"], "2026.9.1")
            self.assertEqual(sent[-1]["params"], {"refreshToken": False})
            process.terminate.assert_called_once()

    def test_optional_account_failure_keeps_valid_quota_without_server_error_body(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"rateLimits": {"primary": {"windowDurationMins": 10080,
                                                                         "usedPercent": 17}}}},
            {"id": 3, "error": {"message": "synthetic sensitive server diagnostic"}},
        ])
        with patch.object(providers, "codex_command", return_value=["codex", "app-server"]), \
                patch.object(providers, "get_version", return_value="2026.9.1"), \
                patch.object(providers.sys, "platform", "linux"), \
                patch.object(providers.subprocess, "Popen", return_value=process):
            rows = providers.fetch_codex()
            self.assertEqual(rows[0]["used"], 17)
            self.assertEqual(rows[0]["subscription_label"], "--")

    def test_windows_launch_uses_no_shell_and_native_process_cleanup(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"rateLimits": {"planType": "pro"}}},
        ])
        with patch.object(providers, "codex_command", return_value=["codex.exe", "app-server"]), \
                patch.object(providers, "get_version", return_value="2026.9.1"), \
                patch.object(providers.sys, "platform", "win32"), \
                patch.object(providers.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(providers.fetch_codex()[0]["subscription_label"], "PRO")
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
            process.terminate.assert_called_once()

    def test_windows_node_cleanup_targets_fixed_numeric_process_tree(self):
        process = self.process([])
        with patch.object(providers.sys, "platform", "win32"), \
                patch.dict(os.environ, {"SYSTEMROOT": str(self.home / "Windows")}), \
                patch.object(providers.subprocess, "run") as run:
            providers._stop_codex(process, ["node.exe", "codex.js", "app-server"])
            self.assertEqual(run.call_args.args[0][1:], ["/PID", "12345", "/T", "/F"])
            self.assertNotIn("shell", run.call_args.kwargs)


class TokenPathPortabilityTests(SyntheticHome):
    def test_native_home_defaults_and_explicit_profile_roots(self):
        self.assertEqual(log_roots(), [(self.home / ".claude/projects", "claude"),
                                     (self.home / ".codex/sessions", "codex"),
                                     (self.home / ".codex/archived_sessions", "codex")])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / "claude profile"),
                                    "CODEX_HOME": str(self.home / "codex profile"),
                                    "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.home / "credentials only")}):
            self.assertEqual(log_roots(), [(self.home / "claude profile/projects", "claude"),
                                         (self.home / "codex profile/sessions", "codex"),
                                         (self.home / "codex profile/archived_sessions", "codex")])

    def test_empty_explicit_roots_do_not_fall_back_to_home(self):
        index = TokenIndex(self.home / "tokens.sqlite3")
        self.addCleanup(index.db.close)
        with patch("meter.tokens.log_roots", side_effect=AssertionError("should not use defaults")):
            self.assertEqual(index.scan([]), set())

    def test_windows_128_bit_file_id_round_trips_and_preserves_incremental_offset(self):
        path = self.home / "usage.jsonl"
        record = {"type": "assistant", "timestamp": "2026-09-20T12:00:00Z",
                  "message": {"id": "synthetic-message", "model": "fable",
                              "usage": {"input_tokens": 20, "output_tokens": 5}}}
        path.write_text(json.dumps(record) + "\n")
        index = TokenIndex(self.home / "tokens.sqlite3")
        self.addCleanup(index.db.close)
        inode = 2 ** 100 + 123
        stat = SimpleNamespace(st_ino=inode, st_size=path.stat().st_size)
        with patch.object(Path, "stat", return_value=stat):
            index.scan_file(path, "claude")
            index.scan_file(path, "claude")
        self.assertEqual(index.db.execute("SELECT inode,offset FROM files").fetchone(),
                         ("inode:" + str(inode), stat.st_size))
        self.assertEqual(index.total("claude", 0, 4102444800), 25)


if __name__ == "__main__":
    unittest.main()
