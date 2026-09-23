"""Read-only provider adapters. Credentials never enter snapshots or the device."""
from __future__ import annotations

import json
import logging
import hashlib
import math
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

from .version import get_version

# A forced refresh (Refresh button / meter button) is honoured immediately,
# except during a server rate-limit backoff and when pressed repeatedly.
FORCE_FLOOR = 10
# Default error backoff, and the bounded range accepted from Retry-After.
ERROR_BACKOFF = 300
RATE_LIMIT_BACKOFF = 900


class ProviderError(RuntimeError):
    """A sanitized adapter error whose text is safe to show a user.

    Messages are fixed strings written here; they never contain credentials,
    HTTP bodies or exception text from third-party libraries.
    """
    code = "error"

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NotSetUp(ProviderError):
    """The provider is not installed or not signed in. This is an absent
    provider, not stale data: its rows are cleared rather than flagged."""
    code = "not_set_up"


class SignInExpired(ProviderError):
    code = "signed_out"


class RateLimited(ProviderError):
    code = "rate_limited"


def epoch(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def window(key, label, duration, used=None, reset=None):
    if isinstance(used, bool) or not isinstance(used, (float, int)) or not math.isfinite(used):
        used = None
    return dict(key=key, label=label, seconds=duration, used=used,
                reset=epoch(reset), tokens=None)


def claude_subscription(credentials):
    plan = credentials.get("subscriptionType")
    if plan == "max":
        return {"default_claude_max_5x": "MAX 5×",
                "default_claude_max_20x": "MAX 20×"}.get(credentials.get("rateLimitTier"), "MAX")
    return {"pro": "PRO", "team": "TEAM", "enterprise": "ENTERPRISE",
            "free": "FREE"}.get(plan, "--")


def parse_claude(data, subscription_label="--"):
    rows = []
    for key, label, duration in [("five_hour", "CLAUDE 5H", 18000),
                                 ("seven_day", "CLAUDE 7D", 604800)]:
        obj = data.get(key)
        obj = obj if isinstance(obj, dict) else {}
        rows.append(window(key, label, duration, obj.get("utilization"), obj.get("resets_at")))
    # Claude Code /usage selects kind=weekly_scoped and scope.model.display_name.
    # Do not infer Fable from an opaque top-level flag or the aggregate weekly %.
    matches = [x for x in (data.get("limits") or []) if isinstance(x, dict)
               and x.get("kind") == "weekly_scoped"
               and str(((x.get("scope") or {}).get("model") or {}).get("display_name", "")).casefold() == "fable"]
    obj = matches[0] if len(matches) == 1 else {}
    rows.append(window("fable", "FABLE 7D", 604800, obj.get("percent"), obj.get("resets_at")))
    for row in rows:
        row["subscription_label"] = subscription_label
    return rows


def parse_codex(data):
    buckets = data.get("rateLimitsByLimitId")
    obj = (buckets.get("codex") if isinstance(buckets, dict) else None) or data.get("rateLimits") or {}
    obj = obj if isinstance(obj, dict) else {}
    # Some accounts expose the weekly quota as primary, with secondary=null.
    weekly = [obj[k] for k in ("primary", "secondary") if isinstance(obj.get(k), dict)
              and obj[k].get("windowDurationMins") == 10080]
    w = weekly[0] if len(weekly) == 1 else {}
    row = window("codex", "CODEX 7D", 604800, w.get("usedPercent"), w.get("resetsAt"))
    row["subscription_label"] = {"pro": "PRO", "plus": "PLUS", "team": "TEAM",
                                 "business": "BUSINESS", "enterprise": "ENTERPRISE",
                                 "edu": "EDU", "free": "FREE", "go": "GO"}.get(obj.get("planType"), "--")
    return [row]


def claude_config_dir():
    """Match Claude's config string, including NFC but not tilde expansion."""
    value = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return Path(unicodedata.normalize("NFC", value))


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))


def claude_credential_store():
    """Return this profile's file root and Keychain service, never another login.

    Storage locations: https://code.claude.com/docs/en/authentication
    Namespace derivation verified in the official Claude Code 2.1.278 build:
    hash the NFC environment string, not a resolved filesystem path. The
    secure-storage override is an internal CLI setting and may change upstream.
    """
    override = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if override is not None:
        value = unicodedata.normalize("NFC", override)
        root = Path(value) if value else Path.home() / ".claude"
    else:
        value = unicodedata.normalize("NFC", os.environ.get("CLAUDE_CONFIG_DIR", ""))
        root = claude_config_dir()
    suffix = "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else ""
    return root, "Claude Code-credentials" + suffix


def _oauth_credentials(raw):
    try:
        document = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        return None
    credentials = document.get("claudeAiOauth") if isinstance(document, dict) else None
    if not isinstance(credentials, dict):
        return None
    token = credentials.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None
    return credentials


def claude_credentials():
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # Do not borrow subscription metadata from an unrelated saved login.
        return {"accessToken": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}
    root, service = claude_credential_store()
    credentials = None
    if sys.platform == "darwin":
        # Capture in memory only. A custom profile must never probe the default
        # service as a fallback; that can display a different account's quota.
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, timeout=20, check=False,
            )
            if result.returncode == 0:
                credentials = _oauth_credentials(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if credentials is None:
        try:
            credentials = _oauth_credentials((root / ".credentials.json").read_bytes())
        except OSError:
            pass
    if credentials is None:
        raise NotSetUp("Claude Code is not signed in (login required for the selected profile). "
                       "Open Claude Code and sign in to show its quota.")
    expires = credentials.get("expiresAt")
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise SignInExpired("Claude Code sign-in details look damaged (login metadata is invalid). "
                                "Open Claude Code and sign in again.")
        if expires <= time.time() * 1000:
            raise SignInExpired("Claude Code sign-in expired. Open Claude Code once to renew it.")
    return credentials


def claude_token():
    return claude_credentials()["accessToken"]


# --- Account identity -------------------------------------------------------
# Switching the Claude Code or Codex account must never show the previous
# account's quota. Each cycle derives a non-secret fingerprint of the signed-in
# account: a salted SHA-256 of stable account/organization ids. Only if a CLI
# stores no ids is a salted hash of its long-lived refresh token used. Neither
# the ids nor any token are stored; only the salted digest is.
#
# A fingerprint is "<kind>:<digest>" (kinds: env, id, app, token) or
# "signed-out". None means "unknown right now" (a config file that exists but
# is being rewritten, a CLI that is not answering): it never causes a switch.

_json_cache = {}
_UNREADABLE = object()  # The file exists but is not (yet) a complete JSON object.


def _read_json_cached(path):
    """Parse a small JSON config again only when its size/mtime changes.

    Returns the object, None when the file does not exist, or _UNREADABLE when
    it exists but cannot be read or parsed (for example mid-write)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        _json_cache.pop(str(path), None)
        return None
    except OSError:
        return _UNREADABLE
    key = (st.st_size, st.st_mtime_ns)
    cached = _json_cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        value = _UNREADABLE
    value = value if isinstance(value, dict) else _UNREADABLE
    _json_cache[str(path)] = (key, value)
    return value


def _digest(salt, *parts):
    material = "\0".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(salt + b"\0" + material).hexdigest()[:24]


def claude_global_config():
    """Claude Code's global config (holds oauthAccount ids, no secrets).

    Returns the object, None when no config exists, or _UNREADABLE when the
    config in use exists but cannot be parsed right now."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates = [claude_config_dir() / ".claude.json"]
    else:
        candidates = [Path.home() / ".claude.json"]
    candidates.append(claude_config_dir() / ".config.json")  # Legacy location.
    for path in candidates:
        document = _read_json_cached(path)
        if document is not None:
            return document  # The first existing file is authoritative, readable or not.
    return None


def claude_account(salt):
    """Fingerprint of the Claude account in use, 'signed-out', or None (unknown)."""
    explicit = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if explicit:
        return "env:" + _digest(salt, "env", explicit)
    config = claude_global_config()
    if config is _UNREADABLE:
        return None  # Mid-write or damaged: never guess another identity.
    account = (config or {}).get("oauthAccount")
    if isinstance(account, dict) and (account.get("accountUuid") or account.get("organizationUuid")):
        return "id:" + _digest(salt, "claude", account.get("accountUuid"), account.get("organizationUuid"))
    try:
        credentials = claude_credentials()
    except NotSetUp:
        return "signed-out"  # No saved sign-in at all: an explicit sign-out.
    except ProviderError:
        return None  # Expired or damaged sign-in: the identity is unknown, not changed.
    secret = credentials.get("refreshToken") or credentials.get("accessToken")
    return "token:" + _digest(salt, "claude-token", secret)


def codex_account(salt):
    """Fingerprint of the Codex login, 'signed-out', or None (unknown).

    auth.json (file credential storage) provides the ChatGPT account id. When
    Codex keeps its login in the OS keyring there is no auth.json; the
    identity then comes from the running `codex app-server` (account/read:
    account type and e-mail/account id, never a token). If the app-server
    reports no identifier (for example an API-key login), the fingerprint is
    unknown and account switches are not detected for that login; quotas are
    still read every minute."""
    path = codex_home() / "auth.json"
    auth = _read_json_cached(path)
    if auth is _UNREADABLE:
        return None
    if auth is not None:
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        if tokens.get("account_id"):
            return "id:" + _digest(salt, "codex", tokens["account_id"])
        secret = tokens.get("refresh_token") or auth.get("OPENAI_API_KEY")
        if secret:
            return "token:" + _digest(salt, "codex-token", secret)
    try:
        account = codex_session().account()
    except NotSetUp:
        return "signed-out"  # Codex is not installed.
    except Exception:  # noqa: BLE001 - identity is best effort
        return None
    if account is None:
        return "signed-out"
    identifiers = [str(account.get(key)) for key in ("accountId", "account_id", "userId", "email")
                   if isinstance(account.get(key), str) and account.get(key)]
    if not identifiers:
        return None
    return "app:" + _digest(salt, "codex-app", account.get("type"), *identifiers)


def account_fingerprints(salt):
    result = {}
    for name, read in (("claude", claude_account), ("codex", codex_account)):
        try:
            result[name] = read(salt)
        except Exception:  # Identity is best effort; never break a refresh.
            result[name] = None
    return result


def user_agent():
    """Identify honestly as Sweetmeter; never as another client."""
    return "Sweetmeter/" + get_version()


def _retry_after(headers):
    try:
        value = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None


def fetch_claude():
    credentials = claude_credentials()
    # The token only ever lives in this request's memory; Sweetmeter never
    # logs, caches or writes it anywhere.
    try:
        response = requests.get("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + credentials["accessToken"],
            "anthropic-beta": "oauth-2025-04-20", "User-Agent": user_agent(),
            "Accept": "application/json"}, timeout=20, allow_redirects=False)
    except requests.Timeout as error:
        raise ProviderError("Claude did not answer in time. Retrying automatically.") from error
    except requests.RequestException as error:
        raise ProviderError("Can't reach Claude. Check the Internet connection; "
                            "Sweetmeter retries automatically.") from error
    try:
        status = response.status_code
        if status == 429:
            raise RateLimited("Claude HTTP 429: Claude asked Sweetmeter to slow down. "
                              "Retrying in a few minutes.", retry_after=_retry_after(response.headers))
        if status in (401, 403):
            raise SignInExpired(f"Claude HTTP {status}: Claude did not accept the saved sign-in. "
                                "Open Claude Code and sign in again.")
        if status != 200:
            raise ProviderError(f"Claude HTTP {status}: Claude's usage service is unavailable. "
                                "Retrying automatically.")
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError("Claude sent an unexpected usage reply. Retrying automatically.") from error
    finally:
        response.close()
    if not isinstance(data, dict) or data.get("error"):
        raise ProviderError("Claude sent an unexpected usage reply. Retrying automatically.")
    return parse_claude(data, claude_subscription(credentials))


def _codex_candidates():
    """Per-user/native installs only; never search WSL or another user's files."""
    home = Path.home()
    if sys.platform == "win32":
        yield home / ".local/bin/codex.exe"
        yield home / ".cargo/bin/codex.exe"
        if os.environ.get("APPDATA"):
            yield Path(os.environ["APPDATA"]) / "npm/codex.cmd"
        if os.environ.get("LOCALAPPDATA"):
            yield Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Links/codex.exe"
    else:
        yield home / ".local/bin/codex"
        yield home / ".cargo/bin/codex"
        yield Path("/opt/homebrew/bin/codex")
        yield Path("/usr/local/bin/codex")
        yield Path("/usr/bin/codex")


def _npm_codex_command(shim):
    """Resolve npm's known package layout without executing a .cmd/.bat shell.

    npm global shims and local node_modules/.bin shims point at the same
    official @openai/codex/bin/codex.js entrypoint. Prefer its native executable
    on Windows so process cleanup also covers a stalled app-server.
    """
    roots = [shim.parent / "node_modules/@openai/codex",
             shim.parent.parent / "@openai/codex"]
    for root in roots:
        entrypoint = root / "bin/codex.js"
        if not entrypoint.is_file():
            continue
        machine = platform.machine().lower()
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        target = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
        # Matches the official npm wrapper's optional platform dependency and
        # legacy bundled vendor fallback, without evaluating shim contents.
        vendors = [root / f"node_modules/@openai/codex-win32-{arch}/vendor",
                   root.parent / f"codex-win32-{arch}/vendor", root / "vendor"]
        for vendor in vendors:
            for binary in (vendor / target / "bin/codex.exe", vendor / target / "codex/codex.exe"):
                if binary.is_file():
                    return [str(binary)]
        node = shim.parent / "node.exe"
        node_name = str(node) if node.is_file() else shutil.which("node.exe") or shutil.which("node")
        if node_name:
            return [node_name, str(entrypoint)]
        raise RuntimeError("Node.js is required for this Codex npm installation")
    raise RuntimeError("Codex npm launcher is unsupported; set SWEETMETER_CODEX_PATH to codex.exe")


def codex_command(binary=None):
    """Return argv, treating an override as one path rather than shell text."""
    explicit = binary or os.environ.get("SWEETMETER_CODEX_PATH")
    if explicit:
        found = shutil.which(str(explicit))
        path = Path(found or str(explicit)).expanduser()
        if not path.is_file():
            raise RuntimeError("Configured Codex executable was not found")
    else:
        found = shutil.which("codex")
        path = Path(found) if found else next((p for p in _codex_candidates() if p.is_file()), None)
        if path is None:
            raise NotSetUp("Codex is not installed on this computer (Codex CLI was not found). "
                           "Install Codex to show its quota, or ignore this if you don't use it.")
    if sys.platform == "win32" and path.suffix.lower() in {".cmd", ".bat"}:
        command = _npm_codex_command(path)
    elif path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise RuntimeError("Use a native Codex executable or its supported npm launcher")
    else:
        command = [str(path)]
    return command + ["app-server"]


def _stop_codex(process, command):
    if process.poll() is None:
        if sys.platform == "win32" and len(command) == 3:
            # Node's child cannot receive SIGTERM on Windows. A fixed executable
            # and numeric PID avoid cmd.exe quoting or command injection.
            system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            try:
                subprocess.run([str(system_root / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        else:
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# `codex app-server` speaks JSON-RPC over stdio and answers any number of
# requests, so one process is kept running and asked once a minute instead of
# starting Codex for every poll. It is restarted when it stops, fails or stops
# answering, when the Codex executable or its login file changes, and at least
# every CODEX_SESSION_MAX_AGE seconds so a long-lived server never serves an
# outdated login (a keyring login changes no file Sweetmeter can watch).
CODEX_SESSION_MAX_AGE = 900
CODEX_TIMEOUT = 30


class CodexSession:
    """One long-lived `codex app-server`; every public method runs on the
    provider worker thread (a lock also serializes stray callers)."""

    def __init__(self, binary=None):
        self.binary = binary
        self.lock = threading.RLock()
        self.process = self.command = self.reader = None
        self.incoming = None
        self.started = 0.0
        self.signature = None
        self.next_id = 0

    # -- process lifetime --------------------------------------------------
    @staticmethod
    def _signature(command):
        """Changes when the executable is replaced or the login file changes."""
        parts = []
        for path in (Path(command[-2]) if len(command) >= 2 else None, codex_home() / "auth.json"):
            try:
                st = path.stat() if path is not None else None
                parts.append((st.st_size, st.st_mtime_ns) if st else None)
            except OSError:
                parts.append(None)
        return tuple(command), tuple(parts)

    def _alive(self):
        return self.process is not None and self.process.poll() is None

    def _start(self, command):
        options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform == "win32" else {}
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                       errors="replace", shell=False, **options)
        except OSError as error:
            raise ProviderError("Cannot start Codex. Reinstall or update Codex; "
                                "Sweetmeter retries automatically.") from error
        incoming = queue.Queue()

        def reader():
            try:
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    # Only responses are kept; notifications and server
                    # requests are not needed and must not pile up.
                    if isinstance(message, dict) and "id" in message and ("result" in message or "error" in message):
                        incoming.put(message)
            except (OSError, ValueError):
                pass  # Shutdown can close the pipe while a read is completing.
            finally:
                incoming.put(None)

        self.process, self.command, self.incoming = process, command, incoming
        self.reader = threading.Thread(target=reader, name="sweetmeter-codex", daemon=True)
        self.reader.start()
        self.started = time.monotonic()
        self.next_id = 0
        self.signature = self._signature(command)
        self._request("initialize", {"clientInfo": {"name": "sweetmeter", "version": get_version()},
                                     "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized", "params": {}})

    def _ensure(self):
        command = codex_command(self.binary)
        if self._alive() and (self.command != command or self._signature(command) != self.signature
                              or time.monotonic() - self.started > CODEX_SESSION_MAX_AGE):
            self.close()
        if not self._alive():
            self.close()
            self._start(command)

    def close(self):
        with self.lock:
            process, command, reader = self.process, self.command, self.reader
            self.process = self.command = self.reader = self.incoming = None
            if process is None:
                return
            try:
                _stop_codex(process, command)
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError, AttributeError):
                        pass
                if reader is not None:
                    reader.join(timeout=1)

    # -- JSON-RPC ------------------------------------------------------------
    def _send(self, obj):
        try:
            self.process.stdin.write(json.dumps(obj) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise ProviderError("Codex stopped answering. Retrying automatically.") from error

    def _request(self, method, params, timeout=None):
        timeout = CODEX_TIMEOUT if timeout is None else timeout
        self.next_id += 1
        number = self.next_id
        self._send({"id": number, "method": method, "params": params})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                message = self.incoming.get(timeout=max(.01, end - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                raise ProviderError("Codex stopped answering. Retrying automatically.")
            if message.get("id") != number:
                continue  # A late answer to a request that already timed out.
            if "error" in message:
                raise ProviderError("Codex could not read its usage limits. Retrying automatically.")
            result = message.get("result", {})
            return result if isinstance(result, dict) else {}
        raise ProviderError("Codex did not answer in time. Retrying automatically.")

    def call(self, method, params, timeout=None):
        """One request; on any failure the process is stopped so the next
        call starts a fresh one."""
        with self.lock:
            try:
                self._ensure()
                return self._request(method, params, timeout)
            except BaseException:
                self.close()
                raise

    # -- queries -------------------------------------------------------------
    def account(self):
        """The signed-in account as reported by Codex (dict), or None when
        signed out. Only used for a salted fingerprint; nothing is stored."""
        account = self.call("account/read", {"refreshToken": False}, timeout=10).get("account")
        return account if isinstance(account, dict) else None

    def rate_limits(self):
        with self.lock:
            try:
                rows = parse_codex(self.call("account/rateLimits/read", {}))
            except ProviderError:
                try:
                    present = self.account() is not None
                except (ProviderError, OSError, ValueError, RuntimeError):
                    present = True  # Unknown: report the original, retryable failure.
                if not present:
                    raise NotSetUp("Codex is installed but not signed in. Run Codex and sign in "
                                   "to show its quota.") from None
                raise
            if rows[0]["subscription_label"] == "--":
                # Older app-server builds provide planType only through account/read.
                # Keep only the plan label; never retain email/account identifiers.
                try:
                    account = self.account()
                    if isinstance(account, dict) and account.get("type") == "chatgpt":
                        rows[0]["subscription_label"] = parse_codex(
                            {"rateLimits": {"planType": account.get("planType")}})[0]["subscription_label"]
                except (RuntimeError, OSError):
                    pass
            return rows


_codex_session = None
_codex_session_lock = threading.Lock()


def codex_session(binary=None):
    global _codex_session
    with _codex_session_lock:
        if _codex_session is None or _codex_session.binary != binary:
            if _codex_session is not None:
                _codex_session.close()
            _codex_session = CodexSession(binary)
        return _codex_session


def close_codex():
    """Stop the long-lived app-server (companion shutdown)."""
    global _codex_session
    with _codex_session_lock:
        session, _codex_session = _codex_session, None
    if session is not None:
        session.close()


def fetch_codex(binary=None):
    return codex_session(binary).rate_limits()


PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}


def describe_error(name, error):
    """Return (code, user-facing message) without exception class names,
    credentials or third-party response text."""
    if isinstance(error, ProviderError):
        return error.code, str(error)
    title = PROVIDER_NAMES.get(name, str(name).title())
    if isinstance(error, (requests.Timeout, TimeoutError, subprocess.TimeoutExpired)):
        return ProviderError.code, f"{title} did not answer in time. Retrying automatically."
    if isinstance(error, requests.RequestException):
        return ProviderError.code, f"Can't reach {title}. Check the Internet connection; retrying automatically."
    if type(error) is RuntimeError:
        # Legacy adapters raise plain RuntimeError with fixed, sanitized text.
        text = str(error)[:200]
        return (RateLimited.code if "429" in text else ProviderError.code), text
    return ProviderError.code, f"{title} usage could not be read. Retrying automatically."


def rate_limited(entry):
    return entry.get("error_code") == RateLimited.code or "429" in str(entry.get("error") or "")


def is_due(entry, now, *, force=False, wake=False, interval=60):
    """Decide whether a provider should be polled now.

    - ``next_poll`` is the normal schedule (or an error backoff).
    - A server 429 backoff is always honoured, even for a forced refresh.
    - A forced refresh bypasses every other wait, including an error backoff,
      so signing in again and pressing Refresh works immediately; repeated
      presses within FORCE_FLOOR seconds are coalesced.
    - ``wake`` shortens a relaxed idle schedule back to ``interval`` when there
      is evidence the quota changed (new local activity, reset nearby).
    """
    next_poll = entry.get("next_poll", 0)
    if not isinstance(next_poll, (int, float)) or next_poll - now > 2 * 86400:
        return True  # Damaged cache or the wall clock moved backwards.
    if now >= next_poll:
        return True
    if rate_limited(entry):
        return False
    if force:
        attempted = entry.get("attempted_at", 0)
        return not isinstance(attempted, (int, float)) or not 0 <= now - attempted < FORCE_FLOOR
    if wake and not entry.get("error"):
        fetched = entry.get("fetched_at", 0)
        return not isinstance(fetched, (int, float)) or now >= fetched + interval
    return False


# Identity bookkeeping kept in a provider's cache entry across polls.
IDENTITY_KEYS = ("account", "account_since", "account_candidate", "last_account", "last_account_since")


def _kind(fingerprint):
    return str(fingerprint).split(":", 1)[0] if fingerprint else None


def switch_account(old, account, now):
    """Return the entry to use when ``account`` is observed.

    A confirmed change drops everything learned for the previous account:
    rows, plan labels, errors and backoff (so it is polled immediately).
    ``account_since`` marks when this account's local token counting starts
    (0 = no known earlier account).

    False switches are avoided: an unknown identity (None) changes nothing, and
    a different fingerprint must be observed twice in a row before it counts
    (a config file caught mid-write, or a fingerprint of another kind such as
    ``token:`` instead of ``id:``, is not proof of a new account). Only an
    explicit sign-out (no saved sign-in at all), and signing in after one,
    take effect at once.
    """
    current = old.get("account")
    if account is None:
        return old
    if current == account:
        if "account_candidate" in old:
            old = {k: v for k, v in old.items() if k != "account_candidate"}
        return old
    known_before = "account" in old
    if known_before and "signed-out" not in (account, current) and old.get("account_candidate") != account:
        return {**old, "account_candidate": account}  # First sighting: confirm next cycle.
    if known_before:
        logging.info("Signed-in account changed; discarding the previous account's cached quota")
    # Local logs carry no account id, so token counting restarts at a real
    # switch. A rotated refresh token (token->token, only when a CLI stores no
    # account ids) cannot be told apart from a switch: its quota is still
    # re-read, but token counting is not cut short. Signing out and back in to
    # the same account resumes that account's count (its own account_since).
    remembered = {k: old[k] for k in ("last_account", "last_account_since") if k in old}
    if current not in (None, "signed-out"):
        remembered = {"last_account": current, "last_account_since": old.get("account_since", 0)}
    if _kind(current) == "token" and _kind(account) == "token":
        since = old.get("account_since", 0)
    elif account == remembered.get("last_account"):
        since = remembered.get("last_account_since", 0)
    else:
        since = now if known_before else 0
    return {"account": account, "account_since": since, **remembered}


def refresh(previous, now=None, readers=None, *, force=False, interval=60, relaxed=None, wake=(),
            accounts=None):
    """Poll due providers. ``relaxed`` maps a provider to a longer idle
    interval; ``wake`` names providers that should use ``interval`` now;
    ``accounts`` maps a provider to its current account fingerprint."""
    now = now or time.time()
    relaxed = relaxed or {}
    accounts = accounts or {}
    providers = dict(previous.get("providers", {})) if isinstance(previous.get("providers"), dict) else {}
    for name, read in (readers or {"claude": fetch_claude, "codex": fetch_codex}).items():
        old = providers.get(name, {})
        if not isinstance(old, dict):
            old = {}
        old = switch_account(old, accounts.get(name), now)
        providers[name] = old
        if not is_due(old, now, force=force, wake=name in wake, interval=interval):
            continue
        identity = {k: old[k] for k in IDENTITY_KEYS if k in old}
        try:
            rows = read()
            providers[name] = {"rows": rows, "fetched_at": now, "attempted_at": now,
                               "next_poll": now + relaxed.get(name, interval),
                               "error": None, "error_code": None, **identity}
        except Exception as error:
            # Only bounded, sanitized adapter messages; never log credentials or HTTP bodies.
            code, message = describe_error(name, error)
            delay = ERROR_BACKOFF
            if code == RateLimited.code:
                retry = getattr(error, "retry_after", None)
                delay = min(3600, max(ERROR_BACKOFF, retry)) if retry else RATE_LIMIT_BACKOFF
            entry = {**old, "error": message, "error_code": code,
                     "attempted_at": now, "next_poll": now + delay}
            if code == NotSetUp.code:
                # Absent, not stale: never keep an old login's quota around.
                for key in ("rows", "fetched_at"):
                    entry.pop(key, None)
            providers[name] = entry
    return {"providers": providers, "updated_at": now}
