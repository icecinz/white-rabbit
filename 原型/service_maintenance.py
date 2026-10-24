"""Finite local busy counts for AdmissionGate.prepare(probe=...).

Call only after admission closed with zero leases. This is not a status refresh:
no Agent, RPC, repair, original message, payload, credential or configuration is
read. Existing stores' ordinary open methods are deliberately not used: some
are read/write connections with multi-second busy timeouts.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

from native_maintenance import count_rows


CAP = 129
COUNT_KEYS = (
    'workers', 'worker_unknown', 'cleanup_blockers', 'cleanup_unknown',
    'runs_pending', 'chat_unknown', 'memory_pending', 'memory_claims', 'memory_unknown',
    'file_tasks_pending', 'file_sources_publishing', 'file_artifacts_publishing', 'file_unknown',
    'publication_publishing', 'publication_unknown', 'refresh_busy',
    'codex_busy', 'codex_unknown', 'pi_busy', 'pi_unknown', 'hermes_busy', 'hermes_unknown',
    'claude_busy', 'claude_unknown', 'dsh_busy', 'dsh_unknown', 'specialist_busy', 'specialist_unknown',
    'interface_tasks_pending', 'interface_tasks_unknown')

# SQL predicates are fixed program text. Only state columns are inspected.
_CHAT = {'runs_pending': ('runs', "status NOT IN ('completed','failed','cancelled','interrupted') OR status IS NULL", ())}
_MEMORY = {
    'memory_pending': ('memory_write_jobs', "state NOT IN ('completed','failed') OR state IS NULL", ()),
    'memory_claims': ('memory_write_jobs', 'claim_token IS NOT NULL', ())}
_FILES = {
    'file_tasks_pending': ('file_tasks', "status NOT IN ('completed','failed','cancelled','interrupted') OR status IS NULL", ()),
    'file_sources_publishing': ('file_sources', "state <> 'verified' OR state IS NULL", ()),
    'file_artifacts_publishing': ('file_artifacts', "state <> 'verified' OR state IS NULL", ())}
_PUBLICATIONS = {'publication_publishing': ('project_publications',
    "state NOT IN ('prepared','verified','conflict','blocked') OR state IS NULL", ())}
_COUNT_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_MANAGERS = (('native_chats', 'codex'), ('pi_native_chats', 'pi'),
             ('hermes_native_chats', 'hermes'), ('claude_native_chats', 'claude'),
             ('dsh_native_chats', 'dsh'))


class _Unknown(ValueError):
    pass


@contextmanager
def _short_lock(lock):
    acquired = False
    try:
        acquired = lock.acquire(False)
        if acquired is not True:
            raise _Unknown()
        yield
    finally:
        if acquired:
            lock.release()


def _identity(value):
    if (type(value) is not tuple or len(value) != 2
            or any(type(item) is not int or item < 0 for item in value)):
        raise _Unknown()
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise _Unknown()
    return min(value, CAP)


def _merge(result, value, keys):
    if (type(value) is not dict or set(value) != {'complete', 'counts'}
            or type(value['complete']) is not bool or type(value['counts']) is not dict
            or set(value['counts']) != set(keys)):
        raise _Unknown()
    for key in keys:
        result[key] = _count(value['counts'][key])
    if not value['complete']:
        raise _Unknown()


def _guarded_root(store):
    root, expected = Path(store.root), _identity(store.guard._root_identity)
    def check():
        if store._closed is not False:
            raise _Unknown()
        actual = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != expected:
            raise _Unknown()
    check()
    return check


def _database(result, owner, specs, unknown, *, path_field, expected, check=None):
    try:
        with _short_lock(owner.lock):
            identity = _identity(expected)
            if getattr(owner, '_closed', False) is not False:
                raise _Unknown()
            value = count_rows(getattr(owner, path_field), specs,
                expected_identity=identity, check_path=check)
            _merge(result, value, specs)
    except Exception:
        result[unknown] = 1


def _native(result, owner, prefix):
    if owner is None:
        return
    try:
        value = owner.maintenance_state()
        if (type(value) is not dict or set(value) != {'complete', 'counts'}
                or type(value['complete']) is not bool or type(value['counts']) is not dict
                or len(value['counts']) > 32 or any(type(key) is not str or not _COUNT_NAME.fullmatch(key)
                                                  for key in value['counts'])):
            raise _Unknown()
        result[prefix + '_busy'] = min(sum(_count(n) for n in value['counts'].values()), CAP)
        if not value['complete']:
            result[prefix + '_unknown'] = 1
    except Exception:
        result[prefix + '_unknown'] = 1


def _publication(result, store):
    if store is None:
        return
    counts = {'publication_publishing': 0}
    try:
        with _short_lock(store.lock):
            check = _guarded_root(store)
            value = count_rows(store.db_path, _PUBLICATIONS,
                expected_identity=_identity(store._db_identity), check_path=check)
            _merge(counts, value, _PUBLICATIONS)
    except Exception:
        result['publication_unknown'] = 1
    result['publication_publishing'] = min(
        result['publication_publishing'] + counts['publication_publishing'], CAP)


def inspect_service(service):
    """Return 30 fixed integer counts; any unavailable evidence stays positive.

    Chat identity must be pinned by Service during startup, before background
    work, as _maintenance_chat_identity=(st_dev, st_ino). Disabled native and
    publication objects are None. Missing required fields are unknown, not idle.
    No physical cleanup or transport-idle claim is made here.
    """
    result = dict.fromkeys(COUNT_KEYS, 0)
    try:
        with _short_lock(service.worker_lock):
            if type(service.workers) is not dict:
                raise _Unknown()
            result['workers'] = _count(len(service.workers))
            blockers = service._cleanup_blockers
            if type(blockers) not in (set, frozenset):
                raise _Unknown()
            result['cleanup_blockers'] = _count(len(blockers))
    except Exception:
        result['worker_unknown'] = result['cleanup_unknown'] = 1

    try:
        store, pin = service.store, _identity(service._maintenance_chat_identity)
        _database(result, store, _CHAT, 'chat_unknown', path_field='path', expected=pin)
    except Exception:
        result['chat_unknown'] = 1

    try:
        jobs = service.memory_jobs
        if os.fspath(jobs.path) != os.fspath(service.store.path):
            raise _Unknown()
        runner = service.memory_runner
        if runner is None:
            _database(result, jobs, _MEMORY, 'memory_unknown', path_field='path',
                      expected=service._maintenance_chat_identity)
        else:
            with _short_lock(runner.execution_lock):
                _database(result, jobs, _MEMORY, 'memory_unknown', path_field='path',
                          expected=service._maintenance_chat_identity)
    except Exception:
        result['memory_unknown'] = 1

    try:
        store = service.file_tasks
        # Root validation opens no content and takes no hidden guard mutex.
        with _short_lock(store.lock):
            check = _guarded_root(store)
            value = count_rows(store.db_path, _FILES,
                expected_identity=_identity(store._db_identity), check_path=check)
            _merge(result, value, _FILES)
    except Exception:
        result['file_unknown'] = 1

    try:
        with _short_lock(service.publication_lock):
            _publication(result, service.publications)
            markdown = getattr(service, 'markdown_chats', None)
            if markdown is not None:
                if markdown.publisher is None:
                    raise _Unknown()
                _publication(result, markdown.publisher)
                sync = getattr(markdown, 'sync', None)
                if sync is not None:
                    with _short_lock(sync.lock):
                        states = sync.state_counts()
                        if type(states) is not dict or set(states) != {'prepared', 'publishing', 'verified', 'conflict', 'blocked'}:
                            raise _Unknown()
                        values = {key: _count(value) for key, value in states.items()}
                        result['publication_publishing'] = min(
                            result['publication_publishing'] + values['publishing'], CAP)
    except Exception:
        result['publication_unknown'] = 1

    try:
        with _short_lock(service.refresh_lock):
            pass
    except Exception:
        result['refresh_busy'] = 1

    for attribute, prefix in _MANAGERS:
        try:
            _native(result, getattr(service, attribute), prefix)
        except Exception:
            result[prefix + '_unknown'] = 1
    try:
        with _short_lock(service._specialist_lock):
            _native(result, service.specialist, 'specialist')
    except Exception:
        result['specialist_unknown'] = 1
    try:
        owner = service.interface_tasks
        if owner is not None:
            value = owner.maintenance_state()
            if (type(value) is not dict or set(value) != {'complete', 'counts'}
                    or type(value['counts']) is not dict or set(value['counts']) != {'active_tasks'}):
                raise _Unknown()
            projected = {'complete': value['complete'],
                'counts': {'interface_tasks_pending': value['counts']['active_tasks']}}
            _merge(result, projected, ('interface_tasks_pending',))
    except Exception:
        result['interface_tasks_unknown'] = 1
    return result
