"""Portable provider tests use synthetic credentials and mocked subprocesses."""
import isolation  # noqa: F401  (test sandbox; must be the first import)
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


class FakeAppServer:
    """A `codex app-server` stand-in speaking line-delimited JSON-RPC.

    `answers` maps a method to a result dict, {"error": ...}, or a callable
    returning either; a method mapped to None is never answered (timeout).
    Every request is recorded in `sent`.
    """
    instances = []

    def __init__(self, answers, pid=12345):
        import queue as queue_module
        self.answers, self.pid = answers, pid
        self.sent, self.exit_code, self.terminated = [], None, 0
        self.lines = queue_module.Queue()
        self.stdin, self.stdout = self.Input(self), self.Output(self)
        FakeAppServer.instances.append(self)

    class Input:
        def __init__(self, server):
            self.server, self.buffer = server, ""

        def write(self, text):
            if self.server.exit_code is not None:
                raise BrokenPipeError("closed")
            self.buffer += text
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                self.server.receive(json.loads(line))

        def flush(self):
            pass

        def close(self):
            pass

    class Output:
        def __init__(self, server):
            self.server = server

        def __iter__(self):
            while True:
                line = self.server.lines.get()
                if line is None:
                    return
                yield line

        def close(self):
            self.server.lines.put(None)

    def receive(self, message):
        self.sent.append(message)
        if "id" not in message:
            return  # A notification such as `initialized`.
        answer = self.answers.get(message["method"], {})
        if callable(answer):
            answer = answer(message)
        if answer is None:
            return
        if isinstance(answer, dict) and "error" in answer:
            reply = {"id": message["id"], "error": answer["error"]}
        else:
            reply = {"id": message["id"], "result": answer}
        # A notification first: it must be ignored, not mistaken for an answer.
        self.lines.put(json.dumps({"method": "account/rateLimits/updated", "params": {}}) + "\n")
        self.lines.put(json.dumps(reply) + "\n")

    def methods(self):
        return [message.get("method") for message in self.sent]

    def poll(self):
        return self.exit_code

    def exit(self, code=1):
        self.exit_code = code
        self.lines.put(None)

    def terminate(self):
        self.terminated += 1
        self.exit(-15)

    kill = terminate

    def wait(self, timeout=None):
        return self.exit_code


WEEKLY = {"rateLimits": {"primary": {"windowDurationMins": 10080, "usedPercent": 17}}}


class CodexRpcTests(SyntheticHome):
    def setUp(self):
        super().setUp()
        FakeAppServer.instances = []
        self.addCleanup(providers.close_codex)

    def serve(self, answers, *, platform="linux", command=("codex", "app-server")):
        """Patch the launch boundary; each Popen starts a new FakeAppServer."""
        popen = Mock(side_effect=lambda *args, **kwargs: FakeAppServer(dict(answers)))
        for patcher in (patch.object(providers, "codex_command", return_value=list(command)),
                        patch.object(providers, "get_version", return_value="2026.9.1"),
                        patch.object(providers.sys, "platform", platform),
                        patch.object(providers.subprocess, "Popen", popen)):
            self.stack.enter_context(patcher)
        return popen

    def test_one_long_lived_app_server_answers_every_poll(self):
        popen = self.serve({"account/rateLimits/read": {"rateLimits": {
            "primary": {"windowDurationMins": 10080, "usedPercent": 17}, "planType": "pro"}}})
        for _ in range(3):
            rows = providers.fetch_codex()
            self.assertEqual((rows[0]["used"], rows[0]["subscription_label"]), (17, "PRO"))
        popen.assert_called_once()  # Started once, asked three times.
        server = FakeAppServer.instances[0]
        self.assertEqual(server.methods(), ["initialize", "initialized"] + ["account/rateLimits/read"] * 3)
        self.assertEqual(server.sent[0]["params"]["clientInfo"]["version"], "2026.9.1")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(server.terminated, 0)
        providers.close_codex()  # Companion shutdown stops it.
        self.assertEqual(server.terminated, 1)

    def test_a_stopped_or_failing_server_is_restarted(self):
        popen = self.serve({"account/rateLimits/read": WEEKLY})
        providers.fetch_codex()
        FakeAppServer.instances[0].exit(1)  # Crashed between polls.
        self.assertEqual(providers.fetch_codex()[0]["used"], 17)
        self.assertEqual(popen.call_count, 2)
        # An error answer stops the process; the next poll starts a fresh one.
        FakeAppServer.instances[1].answers["account/rateLimits/read"] = {"error": {"message": "synthetic"}}
        FakeAppServer.instances[1].answers["account/read"] = {"account": {"type": "chatgpt"}}
        with self.assertRaises(providers.ProviderError):
            providers.fetch_codex()
        self.assertGreaterEqual(FakeAppServer.instances[1].terminated, 1)
        self.assertEqual(providers.fetch_codex()[0]["used"], 17)
        self.assertGreaterEqual(popen.call_count, 3)

    def test_unanswered_request_times_out_and_the_server_is_replaced(self):
        popen = self.serve({"account/rateLimits/read": None, "account/read": {"account": {"type": "chatgpt"}}})
        with patch.object(providers, "CODEX_TIMEOUT", .2):
            with self.assertRaisesRegex(providers.ProviderError, "did not answer"):
                providers.fetch_codex()
        self.assertGreaterEqual(FakeAppServer.instances[0].terminated, 1)
        self.assertGreaterEqual(popen.call_count, 1)

    def test_stale_answer_to_an_earlier_request_is_ignored(self):
        self.serve({"account/rateLimits/read": WEEKLY})
        providers.fetch_codex()
        server = FakeAppServer.instances[0]
        stale = {"rateLimits": {"primary": {"windowDurationMins": 10080, "usedPercent": 99}}}
        server.lines.put(json.dumps({"id": 1, "result": stale}) + "\n")
        self.assertEqual(providers.fetch_codex()[0]["used"], 17)

    def test_changed_login_file_restarts_the_server(self):
        popen = self.serve({"account/rateLimits/read": WEEKLY})
        providers.fetch_codex()
        auth = self.home / ".codex/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("{}")
        providers.fetch_codex()
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(FakeAppServer.instances[0].terminated, 1)

    def test_dynamic_plan_falls_back_to_account_read_and_drops_identity_fields(self):
        self.serve({"account/rateLimits/read": WEEKLY,
                    "account/read": {"account": {"type": "chatgpt", "planType": "plus",
                                                 "email": "synthetic@example.invalid"}}})
        rows = providers.fetch_codex()
        self.assertEqual(rows[0]["subscription_label"], "PLUS")
        self.assertEqual(rows[0]["used"], 17)
        self.assertNotIn("email", json.dumps(rows))
        self.assertEqual(FakeAppServer.instances[0].sent[-1]["params"], {"refreshToken": False})

    def test_optional_account_failure_keeps_valid_quota_without_server_error_body(self):
        self.serve({"account/rateLimits/read": WEEKLY,
                    "account/read": {"error": {"message": "synthetic sensitive server diagnostic"}}})
        rows = providers.fetch_codex()
        self.assertEqual(rows[0]["used"], 17)
        self.assertEqual(rows[0]["subscription_label"], "--")
        self.assertNotIn("synthetic sensitive", json.dumps(rows))

    def test_signed_out_codex_is_not_set_up(self):
        self.serve({"account/rateLimits/read": {"error": {"message": "synthetic"}},
                    "account/read": {"account": None, "requiresOpenaiAuth": True}})
        with self.assertRaises(providers.NotSetUp):
            providers.fetch_codex()

    def test_windows_launch_uses_no_shell_and_native_process_cleanup(self):
        popen = self.serve({"account/rateLimits/read": {"rateLimits": {"planType": "pro"}}},
                           platform="win32", command=("codex.exe", "app-server"))
        self.assertEqual(providers.fetch_codex()[0]["subscription_label"], "PRO")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)
        providers.close_codex()
        self.assertEqual(FakeAppServer.instances[0].terminated, 1)

    def test_windows_node_cleanup_targets_fixed_numeric_process_tree(self):
        process = FakeAppServer({})
        with patch.object(providers.sys, "platform", "win32"), \
                patch.dict(os.environ, {"SYSTEMROOT": str(self.home / "Windows")}), \
                patch.object(providers.subprocess, "run") as run:
            providers._stop_codex(process, ["node.exe", "codex.js", "app-server"])
            self.assertEqual(run.call_args.args[0][1:], ["/PID", "12345", "/T", "/F"])
            self.assertNotIn("shell", run.call_args.kwargs)


class CodexIdentityTests(SyntheticHome):
    """Keyring logins have no auth.json: the app-server supplies the identity."""
    salt = b"s" * 32

    def setUp(self):
        super().setUp()
        FakeAppServer.instances = []
        self.addCleanup(providers.close_codex)

    def serve(self, account):
        answer = account if callable(account) else {"account": account}
        for patcher in (patch.object(providers, "codex_command", return_value=["codex", "app-server"]),
                        patch.object(providers, "get_version", return_value="2026.9.1"),
                        patch.object(providers.subprocess, "Popen",
                                     side_effect=lambda *a, **k: FakeAppServer({"account/read": answer}))):
            self.stack.enter_context(patcher)

    def test_keyring_login_is_fingerprinted_from_the_app_server_without_the_email(self):
        self.serve({"type": "chatgpt", "email": "one@example.invalid", "planType": "plus"})
        first = providers.codex_account(self.salt)
        self.assertTrue(first.startswith("app:"))
        self.assertNotIn("example", first)
        self.assertEqual(first, providers.codex_account(self.salt))
        FakeAppServer.instances[-1].answers["account/read"] = {"account": {"type": "chatgpt",
                                                                            "email": "two@example.invalid"}}
        self.assertNotEqual(first, providers.codex_account(self.salt))
        self.assertEqual(len(FakeAppServer.instances), 1)  # Asked the running server.

    def test_signed_out_unknown_and_missing_codex(self):
        self.serve(None)
        self.assertEqual(providers.codex_account(self.salt), "signed-out")
        providers.close_codex()
        self.stack.close()
        self.setUp()
        self.serve({"type": "apiKey"})  # No identifier: unknown, never a switch.
        self.assertIsNone(providers.codex_account(self.salt))
        with patch.object(providers, "codex_command", side_effect=providers.NotSetUp("missing")):
            providers.close_codex()
            self.assertEqual(providers.codex_account(self.salt), "signed-out")

    def test_file_login_ids_are_preferred_and_damaged_file_is_unknown(self):
        self.serve({"type": "chatgpt", "email": "x@example.invalid"})
        auth = self.home / ".codex/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"tokens": {"account_id": "acct"}}))
        self.assertTrue(providers.codex_account(self.salt).startswith("id:"))
        auth.write_text('{"tokens": ')  # Mid-write.
        self.assertIsNone(providers.codex_account(self.salt))
        self.assertEqual(FakeAppServer.instances, [])  # No process needed for a file login.


class ClaudeIdentityTests(SyntheticHome):
    salt = b"c" * 32

    def test_unreadable_config_is_unknown_not_another_account(self):
        self.credentials(self.home / ".claude", refreshToken="synthetic-refresh")
        config = self.home / ".claude.json"
        config.write_text(json.dumps({"oauthAccount": {"accountUuid": "a", "organizationUuid": "o"}}))
        with patch.object(providers.sys, "platform", "linux"):
            self.assertTrue(providers.claude_account(self.salt).startswith("id:"))
            config.write_text('{"oauthAccount": {"accountUu')  # Caught mid-write.
            self.assertIsNone(providers.claude_account(self.salt))
            config.write_text(json.dumps({"numStartups": 3}))  # No ids: token fingerprint.
            self.assertTrue(providers.claude_account(self.salt).startswith("token:"))
            (self.home / ".claude/.credentials.json").unlink()
            self.assertEqual(providers.claude_account(self.salt), "signed-out")

    def test_expired_sign_in_is_unknown_not_signed_out(self):
        self.credentials(self.home / ".claude", expiresAt=1000)
        with patch.object(providers.sys, "platform", "linux"):
            self.assertIsNone(providers.claude_account(self.salt))


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
