"""Read-only provider adapters. Credentials never enter snapshots or the device."""
from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import requests


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


def claude_credentials():
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"accessToken": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}
    candidates = []
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / ".credentials.json"
    if path.exists():
        try:
            candidates.append(json.loads(path.read_text()).get("claudeAiOauth", {}))
        except (OSError, ValueError):
            pass
    # Capture the credential in memory; never echo security's stdout/stderr.
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                             "Claude Code-credentials", "-w"], capture_output=True, timeout=20)
    if result.returncode == 0:
        try:
            candidates.append(json.loads(result.stdout).get("claudeAiOauth", {}))
        except ValueError:
            pass
    candidates = [c for c in candidates if isinstance(c, dict) and c.get("accessToken")]
    if not candidates:
        raise RuntimeError("Claude login required")
    creds = max(candidates, key=lambda c: c.get("expiresAt") or 0)
    if creds.get("expiresAt", time.time() * 1000 + 1) < time.time() * 1000:
        raise RuntimeError("Claude login expired; open Claude Code")
    return creds


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


def fetch_codex(binary=None):
    binary = binary or shutil.which("codex") or "/opt/homebrew/bin/codex"
    process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    incoming = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                incoming.put(json.loads(line))
            except ValueError:
                pass
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        process.stdin.write(json.dumps(obj) + "\n")
        process.stdin.flush()

    def receive(number):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                msg = incoming.get(timeout=max(.01, end - time.monotonic()))
            except queue.Empty:
                break
            if msg is None:
                break
            if msg.get("id") == number:
                if "error" in msg:
                    raise RuntimeError("Codex account/rateLimits/read failed")
                return msg.get("result", {})
        raise RuntimeError("Codex app-server timeout")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "quota_meter", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return parse_codex(receive(2))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
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
