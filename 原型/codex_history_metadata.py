"""Short, metadata-only native reads for an explicitly selected Codex thread.

The original app-server owns its protocol. This reader never resumes, creates,
submits, answers a tool request, reads a rollout body or changes configuration.
Persisted host-tool evidence comes from an exact read-only SQLite observation,
not from fields omitted by thread/read. Unrelated thread writes are permitted.
"""
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from urllib.parse import quote
import uuid

from rabbit_runtime.codex_rpc import CodexRpc
from rabbit_runtime.process import observe_process_group as _live_group
from native_maintenance import _sidecars
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path


_SHA = re.compile(r'[0-9a-f]{64}\Z')
_REASONS = frozenset(('invalid_service_scope', 'invalid_arguments', 'reader_closed',
    'metadata_busy', 'metadata_timeout', 'metadata_unavailable', 'metadata_schema_unverified',
    'metadata_scope_changed', 'thread_metadata_mismatch', 'dynamic_tools_unsupported',
    'native_home_mismatch', 'native_metadata_unavailable', 'native_metadata_mismatch',
    'native_cleanup_unconfirmed'))


class CodexHistoryMetadataError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in _REASONS else 'metadata_unavailable'
        super().__init__(self.reason)


def _error(reason):
    raise CodexHistoryMetadataError(reason)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        _error('invalid_arguments')


def _remaining(deadline):
    result = deadline - time.monotonic()
    if result <= 0:
        _error('metadata_timeout')
    return result


def _directory_identity(path):
    descriptor = _directory(path)
    try:
        return _identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _file_scope(path):
    parent = _directory(path.parent)
    descriptor = None
    try:
        parent_identity = _identity(os.fstat(parent))
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or _identity(info) != _identity(named)):
            _error('metadata_scope_changed')
        return parent_identity, _identity(info)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


class CodexHistoryMetadata:
    def __init__(self, connection, *, workspace_id, codex_root, rpc_factory=None, timeout=15):
        if (type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)
                or rpc_factory is not None and not callable(rpc_factory)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30):
            _error('invalid_service_scope')
        try:
            self.root = _path(codex_root)
        except ValueError:
            _error('invalid_service_scope')
        self.connection, self.workspace_id = connection, workspace_id
        self._factory, self.timeout = rpc_factory or CodexRpc, float(timeout)
        self._lock = threading.RLock()
        self._retained_rpc, self._closed, self._root_identity = None, False, None

    def _command(self):
        try:
            build = getattr(self.connection, 'command', None)
            command = build(self.workspace_id) if callable(build) else None
            if (not isinstance(command, (list, tuple)) or len(command) != 4
                    or any(type(part) is not str or not part or '\x00' in part for part in command)
                    or list(command[1:]) != ['app-server','--listen','stdio://']
                    or command[0] != 'codex' and (not Path(command[0]).is_absolute()
                                                  or '..' in Path(command[0]).parts)):
                _error('invalid_service_scope')
            return list(command)
        except Exception:
            _error('invalid_service_scope')

    @staticmethod
    def _schema(db):
        required = {'threads': {'id':'TEXT', 'cwd':'TEXT', 'rollout_path':'TEXT', 'archived':'INTEGER'},
                    'thread_dynamic_tools': {'thread_id':'TEXT'}}
        result = {}
        for table, expected in required.items():
            record = db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if (record is None or record[0] != 'table' or type(record[1]) is not str
                    or not record[1].lstrip().upper().startswith('CREATE TABLE ')):
                _error('metadata_schema_unverified')
            columns = {row['name']:row for row in db.execute('PRAGMA table_info(' + table + ')')}
            if any(key not in columns or type(columns[key]['type']) is not str
                   or columns[key]['type'].upper() != kind for key, kind in expected.items()):
                _error('metadata_schema_unverified')
            if table == 'threads' and columns['id']['pk'] != 1:
                _error('metadata_schema_unverified')
            result[table] = columns
        return result

    def _observe(self, thread_id, cwd, deadline):
        db, path = None, self.root/'state_5.sqlite'
        try:
            _remaining(deadline)
            root_identity = _directory_identity(self.root)
            if self._root_identity is None:
                self._root_identity = root_identity
            if root_identity != self._root_identity:
                _error('metadata_scope_changed')
            scope, sidecars = _file_scope(path), _sidecars(path)
            work = _directory_identity(cwd)
            db = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True,
                                 timeout=min(.25, _remaining(deadline)), isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF'); db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda:int(time.monotonic() >= deadline), 1000)
            db.execute('BEGIN')
            schema = self._schema(db)
            extra = [key for key in ('updated_at','updated_at_ms') if key in schema['threads']]
            rows = db.execute('SELECT id,cwd,rollout_path,archived' + ''.join(','+key for key in extra)
                + " FROM threads WHERE id=? AND typeof(cwd)='text' AND length(CAST(cwd AS BLOB))<=8192"
                + " AND typeof(rollout_path)='text' AND length(CAST(rollout_path AS BLOB))<=8192"
                + ''.join(" AND ("+key+" IS NULL OR typeof("+key+")='integer')" for key in extra),
                (thread_id,)).fetchmany(2)
            if len(rows) != 1:
                _error('thread_metadata_mismatch')
            row = dict(rows[0])
            if (row['id'] != thread_id or row['cwd'] != str(cwd) or type(row['archived']) is not int
                    or row['archived'] != 0 or type(row['rollout_path']) is not str
                    or len(row['rollout_path']) > 8192):
                _error('thread_metadata_mismatch')
            rollout = _path(row['rollout_path'])
            if rollout.suffix != '.jsonl' or self.root/'sessions' not in rollout.parents:
                _error('thread_metadata_mismatch')
            rollout_scope = _file_scope(rollout)
            count = db.execute('SELECT COUNT(*) FROM thread_dynamic_tools WHERE thread_id=?',
                               (thread_id,)).fetchone()[0]
            if type(count) is not int or count < 0:
                _error('metadata_schema_unverified')
            if count:
                _error('dynamic_tools_unsupported')
            db.rollback(); db.close(); db = None
            if (scope != _file_scope(path) or sidecars != _sidecars(path)
                    or rollout_scope != _file_scope(rollout) or work != _directory_identity(cwd)
                    or _directory_identity(self.root) != self._root_identity):
                _error('metadata_scope_changed')
            _remaining(deadline)
            return {'row':row, 'count':count, 'database_scope':scope, 'sidecars':sidecars,
                    'cwd_identity':work, 'rollout_scope':rollout_scope}
        except CodexHistoryMetadataError:
            raise
        except (OSError, ValueError, sqlite3.Error):
            _error('metadata_unavailable')
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _thread(value, thread_id, cwd, path):
        thread = value.get('thread') if type(value) is dict else None
        if (type(thread) is not dict or thread.get('id') != thread_id
                or type(thread.get('sessionId')) is not str or not thread['sessionId']
                or len(thread['sessionId']) > 256 or re.search(r'[\s\x00-\x1f\x7f]', thread['sessionId'])
                or thread.get('cwd') != str(cwd) or thread.get('path') != path
                or thread.get('ephemeral') is not False or thread.get('forkedFromId') is not None
                or thread.get('source') not in ('cli','vscode','exec','appServer')
                or thread.get('historyMode') != 'paginated' or thread.get('turns') != []
                or any(key in thread and (type(thread[key]) is not list or thread[key])
                       for key in ('dynamicTools','dynamic_tools'))):
            _error('native_metadata_mismatch')
        try:
            thread['sessionId'].encode('utf-8')
        except UnicodeError:
            _error('native_metadata_mismatch')
        # Do not retain title, preview, instructions, tool descriptions or any
        # unsolicited history/other fields in the response.
        return {key:thread[key] for key in ('id','sessionId','cwd','path','ephemeral','source','historyMode','turns')
                + (('forkedFromId',) if 'forkedFromId' in thread else ())}

    def _close_rpc(self, rpc):
        try:
            rpc.close()
            state = rpc.stats
            if (type(state) is not dict or state.get('process_exited') is not True
                    or state.get('io_threads_stopped') is not True or rpc.running):
                _error('native_cleanup_unconfirmed')
            if os.name == 'posix':
                group = state.get('owned_group_id')
                # Observe only the group created by this RPC. Process/pipe
                # completion alone does not prove this group is empty.
                # A missing group or an uncertain observation is no proof.
                if type(group) is not int or group <= 0 or _live_group(group) is not False:
                    _error('native_cleanup_unconfirmed')
        except Exception:
            self._retained_rpc = rpc
            _error('native_cleanup_unconfirmed')
        self._retained_rpc = None

    def __call__(self, thread_id, cwd, **unsupported):
        if unsupported:
            _error('invalid_arguments')
        thread_id = _uuid(thread_id)
        try:
            cwd = _path(cwd)
            if len(str(cwd)) > 8192:
                _error('invalid_arguments')
        except ValueError:
            _error('invalid_arguments')
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=_remaining(deadline)):
            _error('metadata_busy')
        try:
            if self._closed:
                _error('reader_closed')
            if self._retained_rpc is not None:
                _error('native_cleanup_unconfirmed')
            command = self._command()
            first = self._observe(thread_id, cwd, deadline)
            rpc = None
            try:
                rpc = self._factory(command, cwd=str(cwd),
                                    max_frame_bytes=2*1024*1024, max_events=32)
                self._retained_rpc = rpc
                rpc.start()
                info = rpc.request('initialize', {'clientInfo':{'name':'xiaobaitu-history-metadata',
                    'title':'小白兔', 'version':'0.1.0'}, 'capabilities':{'experimentalApi':True}},
                    timeout=_remaining(deadline))
                if (type(info) is not dict or info.get('codexHome') != str(self.root)
                        or _directory_identity(self.root) != self._root_identity):
                    _error('native_home_mismatch')
                rpc.notify('initialized', {}, timeout=_remaining(deadline))
                thread = self._thread(rpc.request('thread/read', {'threadId':thread_id,'includeTurns':False},
                    timeout=_remaining(deadline)), thread_id, cwd, first['row']['rollout_path'])
            except CodexHistoryMetadataError:
                raise
            except Exception:
                _error('native_metadata_unavailable')
            finally:
                if rpc is not None:
                    self._close_rpc(rpc)
            second = self._observe(thread_id, cwd, deadline)
            if first != second:
                _error('metadata_scope_changed')
            return {'thread':thread, 'persisted_dynamic_tools':{'thread_id':thread_id,'count':0}}
        finally:
            self._lock.release()

    def close(self):
        if not self._lock.acquire(timeout=self.timeout):
            _error('metadata_busy')
        try:
            self._closed = True
            if self._retained_rpc is not None:
                self._close_rpc(self._retained_rpc)
        finally:
            self._lock.release()
