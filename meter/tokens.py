"""Incrementally count usage records, without persisting conversations."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .providers import epoch


def count(value):
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_record(raw, provider, state):
    timestamp = epoch(raw.get("timestamp"))
    if provider == "claude":
        msg = raw.get("message") or {}
        if raw.get("type") != "assistant" or not isinstance(msg, dict):
            return None
        model, usage = msg.get("model", ""), msg.get("usage")
        if not timestamp or not isinstance(usage, dict) or not model or "synthetic" in model:
            return None
        values = [count(usage.get(k)) for k in ("input_tokens", "output_tokens",
                   "cache_creation_input_tokens", "cache_read_input_tokens")]
        identity = msg.get("id")
        if identity:
            identity += ":" + str(raw.get("requestId") or "")
        else:
            identity = raw.get("uuid") or hashlib.sha256(json.dumps([timestamp, model, values]).encode()).hexdigest()
    else:
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if raw.get("type") == "session_meta":
            source = payload.get('source') or {}
            source = source if isinstance(source, dict) else {}
            parent = payload.get('forked_from_id') or payload.get('parent_thread_id') or (
                ((source.get('subagent') or {}).get('thread_spawn') or {}).get('parent_thread_id'))
            state.update(session=payload.get('id') or payload.get('session_id'),
                         parent=parent, replaying=bool(parent))
            return None
        if raw.get("type") == "turn_context":
            state["model"] = payload.get("model", state.get("model", ""))
            return None
        if raw.get("type") != "event_msg" or payload.get("type") != "token_count" or not timestamp:
            return None
        info = payload.get("info") or {}
        usage = info.get("last_token_usage")
        total = info.get("total_token_usage")
        # Rate-limit-only notifications repeat the last usage and cumulative totals.
        if total and total == state.get("total"):
            return None
        if not isinstance(usage, dict):
            if not isinstance(total, dict):
                return None
            previous = state.get('total') or {}
            usage = {k: max(0, count(v) - count(previous.get(k))) for k, v in total.items()}
        state["total"] = total
        model = state.get("model", "unknown")
        cached = min(count(usage.get("input_tokens")), count(usage.get("cached_input_tokens")))
        values = [max(0, count(usage.get("input_tokens")) - cached),
                  count(usage.get("output_tokens")), 0, cached]
        identity = hashlib.sha256(json.dumps([timestamp, usage, total], sort_keys=True).encode()).hexdigest()
    if not sum(values):
        return None
    return (provider, identity, timestamp, model, *values)


class TokenIndex:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        # This is a rebuildable index of local logs, never the source logs themselves.
        if self.db.execute('PRAGMA user_version').fetchone()[0] != 2:
            self.db.executescript('DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS files;'
                                 'DROP TABLE IF EXISTS codex_replay; PRAGMA user_version=2;')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events (
          provider TEXT, id TEXT, ts REAL, model TEXT,
          input INTEGER, output INTEGER, cache_write INTEGER, cache_read INTEGER,
          PRIMARY KEY(provider,id));
        CREATE INDEX IF NOT EXISTS by_time ON events(provider,ts);
        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, state TEXT);
        CREATE TABLE IF NOT EXISTS codex_replay (
          session TEXT, signature TEXT, event_id TEXT, PRIMARY KEY(session, signature));
        ''')

    def scan_file(self, path, provider):
        st = path.stat()
        previous = self.db.execute('SELECT inode,offset,state FROM files WHERE path=?', (str(path),)).fetchone()
        offset, state = 0, {}
        if previous and previous[0] == st.st_ino and previous[1] <= st.st_size:
            offset, state = previous[1], json.loads(previous[2])
        if offset == st.st_size:
            return
        with path.open('rb') as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline(8 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if len(line) < 8 * 1024 * 1024:
                        handle.seek(start)  # Retry an incomplete record on the next scan.
                        break
                    while line and not line.endswith(b'\n'):
                        line = handle.readline(1024 * 1024)
                    continue
                if not any(k in line for k in (b'"usage"', b'"token_count"', b'"turn_context"', b'"session_meta"')):
                    continue
                try:
                    raw = json.loads(line)
                    event = parse_record(raw, provider, state)
                except (ValueError, TypeError, AttributeError):
                    continue
                if event:
                    if provider == 'codex' and state.get('session'):
                        info = raw['payload']['info']
                        signature = hashlib.sha256(json.dumps([
                            info.get('last_token_usage'), info.get('total_token_usage')], sort_keys=True).encode()).hexdigest()
                        if state.get('replaying'):
                            parent = self.db.execute('SELECT event_id FROM codex_replay WHERE session=? AND signature=?',
                                                     (state['parent'], signature)).fetchone()
                            if parent:
                                # Forks can rewrite timestamps; retain the ancestor's event and time.
                                event = self.db.execute('SELECT * FROM events WHERE provider=? AND id=?',
                                                        ('codex', parent[0])).fetchone() or event
                            else:
                                state['replaying'] = False
                        self.db.execute('INSERT OR REPLACE INTO codex_replay VALUES (?,?,?)',
                                        (state['session'], signature, event[1]))
                    self.db.execute('''INSERT INTO events VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(provider,id) DO UPDATE SET
                    input=MAX(events.input,excluded.input), output=MAX(events.output,excluded.output),
                    cache_write=MAX(events.cache_write,excluded.cache_write),
                    cache_read=MAX(events.cache_read,excluded.cache_read)''', event)
            offset = handle.tell()
        self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?)',
                        (str(path), st.st_ino, offset, json.dumps(state)))

    def scan(self, roots=None):
        ch = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude')))
        cx = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
        roots = roots or [(ch/'projects', 'claude'), (Path.home()/'.config/claude/projects', 'claude'),
                          (cx/'sessions', 'codex'), (cx/'archived_sessions', 'codex')]
        available, files, metadata = set(), [], {}
        cutoff = time.time() - 8 * 86400
        for root, provider in roots:
            if not root.is_dir():
                continue
            available.add(provider)
            for path in root.rglob('*.jsonl'):
                try:
                    if path.stat().st_mtime >= cutoff:
                        files.append((path, provider))
                        if provider == 'codex':
                            with path.open('rb') as handle:
                                record = json.loads(handle.readline(8 * 1024 * 1024))
                            state = {}
                            parse_record(record, 'codex', state)
                            if state.get('session'):
                                metadata[state['session']] = (path, state.get('parent'))
                except (FileNotFoundError, PermissionError, ValueError, TypeError, AttributeError):
                    continue
        visited = set()

        def scan_codex(session):
            if session in visited or session not in metadata:
                return
            visited.add(session)
            path, parent = metadata[session]
            scan_codex(parent)
            try:
                self.scan_file(path, 'codex')
            except (FileNotFoundError, PermissionError):
                pass

        for session in metadata:
            scan_codex(session)
        scanned = {entry[0] for entry in metadata.values()}
        for path, provider in files:
            if provider == 'claude' or path not in scanned:
                try:
                    self.scan_file(path, provider)
                except (FileNotFoundError, PermissionError):
                    continue
        self.db.execute('DELETE FROM events WHERE ts < ?', (time.time()-35*86400,))
        self.db.execute("DELETE FROM codex_replay WHERE event_id NOT IN (SELECT id FROM events WHERE provider='codex')")
        self.db.commit()
        return available

    def total(self, provider, start, end, fable=False):
        sql = 'SELECT COALESCE(SUM(input+output+cache_write+cache_read),0) FROM events WHERE provider=? AND ts>=? AND ts<?'
        if fable:
            sql += " AND lower(model) LIKE '%fable%'"
        return self.db.execute(sql, (provider, start, end)).fetchone()[0]
