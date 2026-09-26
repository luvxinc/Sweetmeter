"""Incrementally count usage records, without persisting conversations.

The SQLite file is only a rebuildable index of the CLIs' own local logs. It
must never stop the companion: a damaged file is discarded and rebuilt, and a
file that cannot be read is skipped without aborting the scan.
"""
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from .providers import claude_config_dir, codex_home, epoch

SCHEMA_VERSION = 3
SCAN_DAYS = 8
RETAIN_DAYS = 35
MAX_LINE = 8 * 1024 * 1024
# The session_meta record is cached per file, so this is read at most once.
MAX_META_LINE = 8 * 1024 * 1024
MARKERS = (b'"usage"', b'"token_count"', b'"turn_context"', b'"session_meta"')
# SQLite result codes that mean the file itself is damaged (not busy/full).
CORRUPT_CODES = {11, 26}  # SQLITE_CORRUPT, SQLITE_NOTADB


def log_roots():
    """Use the same native profile roots as each CLI; do not inspect WSL homes."""
    claude = claude_config_dir()
    codex = codex_home()
    return [(claude / 'projects', 'claude'), (codex / 'sessions', 'codex'),
            (codex / 'archived_sessions', 'codex')]


def count(value):
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def is_corrupt(error):
    """True when an sqlite3 error means the index file is unusable."""
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return (code & 0xff) in CORRUPT_CODES
    # Busy/locked/full/I-O are OperationalError; corruption is a bare DatabaseError.
    return type(error) is sqlite3.DatabaseError


def parse_record(raw, provider, state):
    if not isinstance(raw, dict):
        return None
    timestamp = epoch(raw.get("timestamp"))
    if provider == "claude":
        msg = raw.get("message") or {}
        if raw.get("type") != "assistant" or not isinstance(msg, dict):
            return None
        model, usage = msg.get("model", ""), msg.get("usage")
        if not timestamp or not isinstance(usage, dict) or not isinstance(model, str) or not model \
                or "synthetic" in model:
            return None
        values = [count(usage.get(k)) for k in ("input_tokens", "output_tokens",
                   "cache_creation_input_tokens", "cache_read_input_tokens")]
        identity = msg.get("id")
        if identity and isinstance(identity, str):
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
            subagent = source.get('subagent') or {}
            spawn = (subagent.get('thread_spawn') or {}) if isinstance(subagent, dict) else {}
            parent = payload.get('forked_from_id') or payload.get('parent_thread_id') or (
                spawn.get('parent_thread_id') if isinstance(spawn, dict) else None)
            session = payload.get('id') or payload.get('session_id')
            state.update(session=session if isinstance(session, str) else None,
                         parent=parent if isinstance(parent, str) else None,
                         replaying=isinstance(parent, str) and bool(parent))
            return None
        if raw.get("type") == "turn_context":
            model = payload.get("model", state.get("model", ""))
            state["model"] = model if isinstance(model, str) else ""
            return None
        if raw.get("type") != "event_msg" or payload.get("type") != "token_count" or not timestamp:
            return None
        info = payload.get("info") or {}
        if not isinstance(info, dict):
            return None
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
        model = state.get("model") or "unknown"
        cached = min(count(usage.get("input_tokens")), count(usage.get("cached_input_tokens")))
        values = [max(0, count(usage.get("input_tokens")) - cached),
                  count(usage.get("output_tokens")), 0, cached]
        identity = hashlib.sha256(json.dumps([timestamp, usage, total], sort_keys=True).encode()).hexdigest()
    if not sum(values):
        return None
    return (provider, identity, timestamp, model, *values)


def _session_meta(path):
    """Read only the first record of a Codex rollout for its session links.

    Returns None when the first record is still being written (retry later),
    else a (session, parent) pair (either may be None)."""
    with path.open('rb') as handle:
        line = handle.readline(MAX_META_LINE)
    if not line.endswith(b'\n'):
        return None if len(line) < MAX_META_LINE else (None, None)
    state = {}
    try:
        parse_record(json.loads(line), 'codex', state)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return (None, None)
    return state.get('session'), state.get('parent')


def _walk(root, provider, cutoff):
    """Yield (path, stat) for recent *.jsonl files, never raising for one entry.

    Codex stores rollouts under sessions/YYYY/MM/DD; whole old date folders are
    pruned without being listed, so the per-minute walk stays small."""
    oldest = datetime.date.fromtimestamp(cutoff) - datetime.timedelta(days=2)

    def old_folder(parts):
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return False
        if len(numbers) == 1:
            return numbers[0] < oldest.year
        if len(numbers) == 2:
            return tuple(numbers) < (oldest.year, oldest.month)
        if len(numbers) == 3:
            try:
                return datetime.date(*numbers) < oldest
            except ValueError:
                return False
        return False

    for folder, dirs, names in os.walk(root, onerror=lambda _error: None):
        if provider == 'codex':
            base = Path(folder).relative_to(root).parts
            dirs[:] = [d for d in dirs if not old_folder(base + (d,))]
        for name in names:
            if not name.endswith('.jsonl'):
                continue
            path = Path(folder) / name
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                yield path, st


class TokenIndex:
    def __init__(self, path):
        self.path = None if str(path) == ':memory:' else Path(path)
        self.db = None
        self.recovered = False
        self.skipped = 0
        self.activity = {}
        self._open()

    # -- lifecycle -------------------------------------------------------
    def _connect(self, target):
        db = sqlite3.connect(str(target))
        try:
            # This is a rebuildable index of local logs, never the source logs themselves.
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                db.executescript('DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS files;'
                                 'DROP TABLE IF EXISTS codex_replay;'
                                 f'PRAGMA user_version={SCHEMA_VERSION};')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
              provider TEXT, id TEXT, ts REAL, model TEXT,
              input INTEGER, output INTEGER, cache_write INTEGER, cache_read INTEGER,
              PRIMARY KEY(provider,id));
            CREATE INDEX IF NOT EXISTS by_time ON events(provider,ts);
            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY, inode TEXT, offset INTEGER, state TEXT,
              size INTEGER, mtime INTEGER, meta INTEGER DEFAULT 0, session TEXT, parent TEXT);
            CREATE TABLE IF NOT EXISTS codex_replay (
              session TEXT, signature TEXT, event_id TEXT, PRIMARY KEY(session, signature));
            ''')
            db.execute('SELECT COUNT(*) FROM files').fetchone()
        except BaseException:
            db.close()
            raise
        return db

    def _discard(self):
        if self.path is None:
            return
        for suffix in ('', '-journal', '-wal', '-shm'):
            try:
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    def _open(self):
        target = self.path if self.path is not None else ':memory:'
        try:
            self.db = self._connect(target)
            return
        except sqlite3.DatabaseError:
            logging.warning('Token index is damaged or unreadable; rebuilding it from local logs')
        self.recovered = True
        self._discard()
        try:
            self.db = self._connect(target)
        except sqlite3.DatabaseError:
            # Never block startup: keep counting in memory for this session.
            logging.warning('Token index cannot be stored on disk; using a temporary index')
            self.db = self._connect(':memory:')

    def rebuild(self):
        """Throw the index away and start over. It is derived data only."""
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        self.recovered = True
        self._discard()
        self._open()

    def close(self):
        self.db.close()

    # -- scanning --------------------------------------------------------
    def scan_file(self, path, provider, st=None, meta=None):
        st = st or path.stat()
        size = st.st_size
        mtime = getattr(st, 'st_mtime_ns', None)
        previous = self.db.execute('SELECT inode,offset,state FROM files WHERE path=?', (str(path),)).fetchone()
        offset, state = 0, {}
        # Windows can expose 128-bit file IDs, beyond SQLite's signed INTEGER.
        # A tagged decimal string stays exact even in the legacy INTEGER column.
        inode = 'inode:' + str(st.st_ino)
        if previous and previous[0] in (st.st_ino, inode) and isinstance(previous[1], int) \
                and 0 <= previous[1] <= size:
            try:
                saved = json.loads(previous[2])
            except (TypeError, ValueError):
                saved = None
            if isinstance(saved, dict):
                offset, state = previous[1], saved
            # A damaged state restarts the file; inserts below are idempotent.
        if offset != size:
            offset = self._read(path, provider, offset, state)
        if meta is not None:
            known, (session, parent) = 1, meta
        else:
            # Reading from the start parsed session_meta if the file has one.
            known = 1 if provider != 'codex' or 'session' in state else 0
            session, parent = state.get('session'), state.get('parent')
        self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)',
                        (str(path), inode, offset, json.dumps(state), size, mtime,
                         known, session, parent))

    def _read(self, path, provider, offset, state):
        with path.open('rb') as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline(MAX_LINE)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if len(line) < MAX_LINE:
                        handle.seek(start)  # Retry an incomplete record on the next scan.
                        break
                    while line and not line.endswith(b'\n'):
                        line = handle.readline(1024 * 1024)
                    continue
                if not any(k in line for k in MARKERS):
                    continue
                try:
                    raw = json.loads(line)
                    event = parse_record(raw, provider, state)
                except (ValueError, TypeError, AttributeError, RecursionError):
                    continue  # UnicodeDecodeError is a ValueError.
                if event:
                    self._store(provider, raw, event, state)
            return handle.tell()

    def _store(self, provider, raw, event, state):
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

    def scan(self, roots=None):
        """Scan changed log files and return the providers whose logs exist.

        Files whose inode, size and mtime match the index are skipped without
        being opened. One unreadable or malformed file never aborts the scan.
        Database errors propagate so the caller can rebuild the index.
        """
        roots = log_roots() if roots is None else roots
        available, files = set(), []
        self.skipped = 0
        cutoff = time.time() - SCAN_DAYS * 86400
        for root, provider in roots:
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
            available.add(provider)
            files.extend((path, st, provider) for path, st in _walk(root, provider, cutoff))
        known = {row[0]: row[1:] for row in self.db.execute(
            'SELECT path,inode,size,mtime,meta,session,parent FROM files')}
        changed, sessions = [], {}
        for path, st, provider in files:
            key = str(path)
            row = known.get(key)
            same_file = row is not None and row[0] in (st.st_ino, 'inode:' + str(st.st_ino))
            meta = (row[4], row[5]) if same_file and row[3] else None
            unchanged = same_file and row[1] == st.st_size and row[2] == getattr(st, 'st_mtime_ns', None)
            if provider == 'codex' and meta is None:
                try:
                    meta = _session_meta(path)
                except OSError:
                    self.skipped += 1
                    continue
                if meta is not None and unchanged:
                    # Cache it so the (possibly large) first line is read once.
                    self.db.execute('UPDATE files SET meta=1, session=?, parent=? WHERE path=?',
                                    (meta[0], meta[1], key))
            if not unchanged:
                changed.append((path, st, provider, meta))
                self.activity[provider] = max(self.activity.get(provider, 0), st.st_mtime)
            if provider == 'codex' and meta and meta[0]:
                sessions[meta[0]] = (key, meta[1])
        # Scan Codex parents before forks, iteratively (fork chains can be deep).
        rank, order = {}, 0
        for session in sessions:
            chain = []
            while session in sessions and sessions[session][0] not in rank and session not in chain:
                chain.append(session)
                session = sessions[session][1]
            for item in reversed(chain):
                rank[sessions[item][0]] = order
                order += 1
        changed.sort(key=lambda entry: (entry[2] != 'codex', rank.get(str(entry[0]), order)))
        for path, st, provider, meta in changed:
            try:
                self.scan_file(path, provider, st, meta if provider == 'codex' else None)
            except OSError:
                self.skipped += 1  # Unreadable, locked or vanished; retry next scan.
        if self.skipped:
            logging.info('Token scan skipped %d unreadable log file(s)', self.skipped)
        seen = [str(entry[0]) for entry in files]
        self.db.execute('CREATE TEMP TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY)')
        self.db.execute('DELETE FROM seen')
        self.db.executemany('INSERT OR IGNORE INTO seen VALUES (?)', ((p,) for p in seen))
        # Forget files that aged out or vanished; a later append rescans idempotently.
        self.db.execute('DELETE FROM files WHERE path NOT IN (SELECT path FROM seen)')
        self.db.execute('DELETE FROM events WHERE ts < ?', (time.time() - RETAIN_DAYS * 86400,))
        self.db.execute("DELETE FROM codex_replay WHERE event_id NOT IN (SELECT id FROM events WHERE provider='codex')")
        self.db.commit()
        return available

    def total(self, provider, start, end, fable=False):
        sql = 'SELECT COALESCE(SUM(input+output+cache_write+cache_read),0) FROM events WHERE provider=? AND ts>=? AND ts<?'
        if fable:
            sql += " AND lower(model) LIKE '%fable%'"
        return self.db.execute(sql, (provider, start, end)).fetchone()[0]
