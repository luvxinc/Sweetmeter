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

_json_cache = {}


def _read_json_cached(path):
    """Parse a small JSON config again only when its size/mtime changes."""
    try:
        st = path.stat()
    except OSError:
        _json_cache.pop(str(path), None)
        return None
    key = (st.st_size, st.st_mtime_ns)
    cached = _json_cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        value = None
    value = value if isinstance(value, dict) else None
    _json_cache[str(path)] = (key, value)
    return value


def _digest(salt, *parts):
    material = "\0".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(salt + b"\0" + material).hexdigest()[:24]


def claude_global_config():
    """Claude Code's global config (holds oauthAccount ids, no secrets)."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates = [claude_config_dir() / ".claude.json"]
    else:
        candidates = [Path.home() / ".claude.json"]
    candidates.append(claude_config_dir() / ".config.json")  # Legacy location.
    for path in candidates:
        document = _read_json_cached(path)
        if document is not None:
            return document
    return None


def claude_account(salt):
    """Fingerprint of the Claude account in use, or 'signed-out'."""
    explicit = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if explicit:
        return "env:" + _digest(salt, "env", explicit)
    config = claude_global_config() or {}
    account = config.get("oauthAccount")
    if isinstance(account, dict) and (account.get("accountUuid") or account.get("organizationUuid")):
        return "id:" + _digest(salt, "claude", account.get("accountUuid"), account.get("organizationUuid"))
    try:
        credentials = claude_credentials()
    except ProviderError:
        return "signed-out"
    secret = credentials.get("refreshToken") or credentials.get("accessToken")
    return "token:" + _digest(salt, "claude-token", secret)


def codex_account(salt):
    """Fingerprint of the Codex login, or None when it is not observable
    (for example credentials kept in the OS keyring)."""
    auth = _read_json_cached(codex_home() / "auth.json")
    if auth is None:
        return None if (codex_home() / "auth.json").exists() else "signed-out"
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if tokens.get("account_id"):
        return "id:" + _digest(salt, "codex", tokens["account_id"])
    secret = tokens.get("refresh_token") or auth.get("OPENAI_API_KEY")
    if secret:
        return "token:" + _digest(salt, "codex-token", secret)
    return "signed-out"


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


def fetch_codex(binary=None):
    command = codex_command(binary)
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
                    incoming.put(json.loads(line))
                except ValueError:
                    pass
        except (OSError, ValueError):
            pass  # Shutdown can close the pipe while a read is completing.
        finally:
            incoming.put(None)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def send(obj):
        process.stdin.write(json.dumps(obj) + "\n")
        process.stdin.flush()

    def receive(number, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = incoming.get(timeout=max(.01, end - time.monotonic()))
            except queue.Empty:
                break
            if msg is None:
                break
            if isinstance(msg, dict) and msg.get("id") == number:
                if "error" in msg:
                    raise ProviderError("Codex could not read its usage limits. Retrying automatically.")
                result = msg.get("result", {})
                return result if isinstance(result, dict) else {}
        raise ProviderError("Codex did not answer in time. Retrying automatically.")

    def signed_in():
        """Only the account's presence is inspected; identifiers are discarded."""
        send({"id": 4, "method": "account/read", "params": {"refreshToken": False}})
        return isinstance(receive(4, timeout=5).get("account"), dict)

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "sweetmeter", "version": get_version()},
            "capabilities": {"experimentalApi": True}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        try:
            rows = parse_codex(receive(2))
        except ProviderError:
            try:
                present = signed_in()
            except (ProviderError, OSError, ValueError):
                present = True  # Unknown: report the original, retryable failure.
            if not present:
                raise NotSetUp("Codex is installed but not signed in. Run Codex and sign in "
                               "to show its quota.") from None
            raise
        if rows[0]["subscription_label"] == "--":
            # Older app-server builds provide planType only through account/read.
            # Keep only the plan label; never retain email/account identifiers.
            try:
                send({"id": 3, "method": "account/read", "params": {"refreshToken": False}})
                account = receive(3, timeout=5).get("account")
                if isinstance(account, dict) and account.get("type") == "chatgpt":
                    rows[0]["subscription_label"] = parse_codex({"rateLimits": {"planType": account.get("planType")}})[0]["subscription_label"]
            except (RuntimeError, OSError):
                pass
        return rows
    finally:
        try:
            _stop_codex(process, command)
        finally:
            process.stdin.close()
            reader_thread.join(timeout=1)
            process.stdout.close()


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


def switch_account(old, account, now):
    """Return the entry to use for ``account``. A changed fingerprint drops
    everything learned for the previous account: rows, plan labels, errors
    and backoff (so it is polled immediately). ``account_since`` marks when
    this account's local token counting starts (0 = no known earlier account)."""
    if account is None or old.get("account") == account:
        return old
    previous = old.get("account")
    known_before = "account" in old
    if known_before:
        logging.info("Signed-in account changed; discarding the previous account's cached quota")
    # Local logs carry no account id, so token counting restarts at a real
    # switch. A rotated refresh token (token->token, only when a CLI stores no
    # account ids) cannot be told apart from a switch: its quota is still
    # re-read, but token counting is not cut short.
    rotated = str(previous).startswith("token:") and account.startswith("token:")
    since = old.get("account_since", 0) if rotated else (now if known_before else 0)
    return {"account": account, "account_since": since}


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
        identity = {k: old[k] for k in ("account", "account_since") if k in old}
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
