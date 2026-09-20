"""Read-only provider adapters. Credentials never enter snapshots or the device."""
from __future__ import annotations

import json
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
        obj = data.get(key) or {}
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
        raise RuntimeError("Claude login required for the selected profile")
    expires = credentials.get("expiresAt")
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise RuntimeError("Claude login metadata is invalid; open Claude Code")
        if expires <= time.time() * 1000:
            raise RuntimeError("Claude login expired; open Claude Code")
    return credentials


def claude_token():
    return claude_credentials()["accessToken"]


def fetch_claude():
    credentials = claude_credentials()
    response = requests.get("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + credentials["accessToken"],
        "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.121",
        "Accept": "application/json"}, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError(f"Claude HTTP {response.status_code}")
    data = response.json()
    if data.get("error"):
        raise RuntimeError("Claude usage endpoint error")
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
            raise RuntimeError("Codex CLI was not found; install it or set SWEETMETER_CODEX_PATH")
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
        raise RuntimeError("Cannot start Codex CLI; check its executable and installation") from error
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
                    raise RuntimeError("Codex account/rateLimits/read failed")
                result = msg.get("result", {})
                return result if isinstance(result, dict) else {}
        raise RuntimeError("Codex app-server timeout")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "sweetmeter", "version": get_version()},
            "capabilities": {"experimentalApi": True}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        rows = parse_codex(receive(2))
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


def refresh(previous, now=None, readers=None, *, force=False, interval=60):
    now = now or time.time()
    providers = dict(previous.get("providers", {}))
    for name, read in (readers or {"claude": fetch_claude, "codex": fetch_codex}).items():
        old = providers.get(name, {})
        if now < old.get("next_poll", 0) and (not force or old.get("error")):
            continue
        try:
            rows = read()
            providers[name] = {"rows": rows, "fetched_at": now, "next_poll": now + interval, "error": None}
        except Exception as error:
            # Only bounded, sanitized adapter messages; never log credentials or HTTP bodies.
            message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            delay = 900 if "429" in message else 300
            providers[name] = {**old, "error": message, "next_poll": now + delay}
    return {"providers": providers, "updated_at": now}
