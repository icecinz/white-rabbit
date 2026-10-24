"""Bounded local runtime observations; no RPC, native reads or repair.

Metadata reads use explicit paths and nonblocking locks. Incomplete observations
stay unsafe to treat as idle and never replace the embedding host's admission
barrier. The holder's private state is inspected only inside this package.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote


LIMIT = 129


COUNT_NAMES = ('rpc_retained', 'sessions_retained', 'readers_alive', 'reserved', 'active',
    'pending_requests', 'unknown_requests', 'continuation_jobs', 'configuration_pending',
    'pending_creations', 'pending_prompts', 'unknown_controls', 'pending_profile_applications',
    'pending_native_drafts', 'pending_attachments', 'closing')


_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


@contextmanager
def try_locks(*locks):
    acquired = []
    try:
        for lock in locks:
            if lock is None or not lock.acquire(False):
                yield False
                return
            acquired.append(lock)
        yield True
    finally:
        for lock in reversed(acquired): lock.release()


def file_identity(path):
    """No-follow regular-file identity, without reading its contents."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Untrusted metadata path.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1: raise ValueError('Untrusted metadata file.')
        return value.st_dev, value.st_ino
    finally: os.close(fd)


def sidecar_identities(path):
    present = {}
    for suffix in ('-journal', '-wal', '-shm'):
        candidate = Path(str(path)+suffix)
        try: value = candidate.lstat()
        except FileNotFoundError: continue
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ValueError('Untrusted metadata sidecar.')
        present[suffix] = (value.st_dev, value.st_ino)
    # A read-only WAL connection may otherwise create its absent shared-memory
    # sidecar. Do not recover, checkpoint or manufacture one for maintenance.
    if '-wal' in present and '-shm' not in present: raise ValueError('WAL state cannot be read without creating metadata.')
    return present


def count_rows(path, specs, *, lock=None, expected_identity=None, check_path=None, flock_fd=None,
        optional_tables=()):
    """Read capped counts from fixed caller-owned SQL predicates, or unknown.

    specs maps fixed names to (real_table, fixed_where, bound_parameters). It must
    come from trusted source code, never HTTP or configuration. No rows/IDs/bodies
    are loaded. An optional table may be absent, but cannot be a view or virtual
    table. NativeSessionStore alone uses a temporary nonblocking flock; other
    native stores retain their existing exclusive lifetime lock unchanged.
    """
    counts = {name:0 for name in specs}
    result = {'complete':False, 'counts':counts}
    db = fd = None
    flocked = locked = False
    try:
        if not isinstance(specs, dict) or len(specs) > 32 or any(not _NAME.fullmatch(name) for name in specs): return result
        if lock is not None:
            if not lock.acquire(False): return result
            locked = True
        if check_path is not None: check_path()
        if flock_fd is not None:
            fcntl.flock(flock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB); flocked = True
        path = Path(path)
        identity = file_identity(path)
        sidecars = sidecar_identities(path)
        if expected_identity is not None and identity != tuple(expected_identity): return result
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != identity: return result
        db = sqlite3.connect('file:'+quote(str(path), safe='/')+'?mode=ro', uri=True, timeout=0, isolation_level=None)
        db.execute('PRAGMA busy_timeout=0'); db.execute('PRAGMA trusted_schema=OFF'); db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic()+.025
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        db.execute('BEGIN')
        for name, (table, where, parameters) in specs.items():
            if not _NAME.fullmatch(table) or type(where) is not str or any(value in where for value in (';', '--', '/*')):
                return result
            schema = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
            if schema is None and table in optional_tables: continue
            if schema is None or schema[0] != 'table' or type(schema[1]) is not str or not schema[1].lstrip().upper().startswith('CREATE TABLE '):
                return result
            sql = 'SELECT COUNT(*) FROM (SELECT 1 FROM '+table+(' WHERE '+where if where else '')+' LIMIT 129)'
            value = db.execute(sql, parameters).fetchone()[0]
            if type(value) is not int or not 0 <= value <= LIMIT: return result
            counts[name] = value
        if file_identity(path) != identity: return result
        if sidecar_identities(path) != sidecars: return result
        if check_path is not None: check_path()
        result['complete'] = True
        return result
    except Exception:
        return result
    finally:
        if db is not None:
            try: db.close()
            except Exception: result['complete'] = False
        if fd is not None: os.close(fd)
        if flocked:
            try: fcntl.flock(flock_fd, fcntl.LOCK_UN)
            except Exception: result['complete'] = False
        if locked: lock.release()


def merge_observation(result, value):
    result['complete'] = result['complete'] and value['complete']
    for name, count in value['counts'].items():
        if name not in result['counts'] or type(count) is not int or count < 0:
            result['complete'] = False
        else: result['counts'][name] = min(LIMIT, result['counts'][name]+count)


def codex_holder_state(holder, *, include_ledger=True):
    """Observe this owner only; a shared ledger is optional for host aggregation.

    Order and scope match the existing observer: actions, display cache, then
    session lock; all acquisitions are nonblocking. Ledger metadata is observed
    after those locks are released. No transport property, RPC or private body
    is read, and inability to observe stays incomplete.
    """
    result = {'complete': True, 'counts': dict.fromkeys(COUNT_NAMES, 0)}
    try:
        if type(include_ledger) is not bool:
            result['complete'] = False
            return result
        with try_locks(holder._actions, holder._cache) as acquired:
            if not acquired:
                result['complete'] = False
            else:
                counts = result['counts']
                counts['rpc_retained'] += int(holder._rpc is not None)
                counts['readers_alive'] += int(holder._reader is not None and holder._reader.is_alive())
                active = holder._state == 'opening'
                session = holder._session
                counts['sessions_retained'] += int(session is not None)
                turn = holder._turn_state
                active = active or (holder._turn_client_id is not None and (turn is None
                    or turn.get('status') not in ('completed', 'failed', 'interrupted')))
                counts['pending_requests'] += len(holder._tokens)
                counts['unknown_requests'] += sum(not row.get('resolved_by_server', False)
                    and (token in holder._tokens or token in holder._submitted_keys.values())
                    for token, row in holder._answers.items())
                if session is not None:
                    with try_locks(session._lock) as acquired:
                        if acquired:
                            counts['pending_requests'] += len(session._requests)
                        else:
                            result['complete'] = False
                counts['active'] += int(active)
        if include_ledger:
            merge_observation(result, holder.store.maintenance_state())
    except Exception:
        result['complete'] = False
    result['counts'] = {name: min(LIMIT, value) for name, value in result['counts'].items()}
    return result
