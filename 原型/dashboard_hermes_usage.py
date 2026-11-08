"""Read the recorded main usage for one published Hermes chat."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

from dashboard_usage import ReadFailure, SourceContext, _SANDBOX, _blocked, _fingerprint
from rabbit_runtime.directory_identity import FilePin
from rabbit_runtime.hermes.session_store import validate_session_id
from rabbit_runtime.maintenance import file_identity as catalogue_identity
from rabbit_runtime.native_session_store import file_identity, open_directory, validate_path


_FIELDS = (
    ('input_tokens', '输入 Token', 'Token'),
    ('output_tokens', '输出 Token', 'Token'),
    ('cache_read_tokens', '缓存读取 Token', 'Token'),
    ('cache_write_tokens', '缓存写入 Token', 'Token'),
    ('reasoning_tokens', '推理 Token', 'Token'),
    ('api_call_count', 'API 调用次数', '次'),
)
_SUFFIXES = ('', '-wal', '-shm', '-journal')
_CHANGED = 'Hermes 会话或记录文件发生变化，请重新读取。'
_SANDBOX_PROFILE = '(version 1)(allow default)(deny file-write*)(deny network*)'

# An ordinary read-only SQLite connection can still create or alter WAL shared
# memory. Keep those writes denied at the OS level. Immutable mode is safe only
# when the captured database has no pending journal content.
_QUERY = '''
from contextlib import closing
from pathlib import Path
import json, sqlite3, sys

path, mode, session_id, cwd, *fields = sys.argv[1:]
with closing(sqlite3.connect(Path(path).as_uri() + mode, uri=True, timeout=1)) as db:
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('BEGIN')
    table = db.execute("SELECT type, sql FROM sqlite_master WHERE name='sessions'").fetchone()
    if not table or table[0] != 'table' or not table[1].lstrip().upper().startswith('CREATE TABLE '):
        sys.exit(2)
    columns = db.execute('PRAGMA table_info(sessions)').fetchall()
    if [column[1] for column in columns if column[5]] != ['id']:
        sys.exit(2)
    present = [field for field in fields if any(column[1] == field for column in columns)]
    sql = 'SELECT id, cwd' + ''.join(', ' + field for field in present) + ' FROM sessions WHERE id=?'
    row = db.execute(sql, (session_id,)).fetchone()
    if row is None or row[:2] != (session_id, cwd):
        sys.exit(3)
    print(json.dumps({field: value for field, value in zip(present, row[2:]) if type(value) is int}))
'''


@contextmanager
def _profile_directory(binding):
    home = validate_path(binding['profile_home'])
    descriptor = open_directory(home)
    try:
        if file_identity(os.fstat(descriptor)) != (binding['home_device'], binding['home_inode']):
            raise ValueError('Hermes profile identity changed.')
        yield home, descriptor
    finally:
        os.close(descriptor)


def _file_state(descriptor, suffix='', *, optional=False):
    try:
        info = os.stat('state.db' + suffix, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError('Hermes metadata file is not an owned regular file.')
    return (*file_identity(info), info.st_size, info.st_ctime_ns, info.st_mtime_ns)


def _state(descriptor):
    return tuple(_file_state(descriptor, suffix, optional=bool(suffix)) for suffix in _SUFFIXES)


def _catalogue_current(manager):
    if catalogue_identity(manager.catalogue.path) != manager._maintenance_catalogue_identity:
        raise ValueError('Hermes catalogue identity changed.')


def _read(manager, identity, binding, record_anchor):
    try:
        _catalogue_current(manager)
        manager.catalogue.get(identity)
        if manager.store.current_binding(identity) != binding:
            raise ReadFailure(_CHANGED)
        with _profile_directory(binding) as (home, descriptor), FilePin(home / 'state.db', record_anchor) as record:
            before = _state(descriptor)
            if before[0][:2] != record.current_identity:
                raise ReadFailure(_CHANGED)
            _, wal, shm, journal = before
            if (journal and journal[2]) or (wal and wal[2] and not shm):
                raise ReadFailure('Hermes 记录的日志状态暂时无法安全读取，请稍后更新。', 'unavailable')
            mode = '?mode=ro' if wal and wal[2] else '?mode=ro&immutable=1'
            command = [_SANDBOX, '-p', _SANDBOX_PROFILE, sys.executable, '-I', '-B', '-c', _QUERY,
                       str(home / 'state.db'), mode, binding['session_key'], binding['cwd'],
                       *(field for field, _, _ in _FIELDS)]
            result = subprocess.run(command, cwd='/', env={}, capture_output=True, timeout=5)
            _catalogue_current(manager)
            manager.catalogue.get(identity)
            if manager.store.current_binding(identity) != binding or _state(descriptor) != before:
                raise ReadFailure(_CHANGED)
        if result.returncode == 3:
            raise ReadFailure('找不到这段 Hermes 会话的原生用量记录。', 'unavailable')
        if result.returncode != 0:
            raise ReadFailure('Hermes 原生用量记录暂时无法读取。')
        values = json.loads(result.stdout)
    except ReadFailure:
        raise
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        raise ReadFailure('Hermes 会话或记录文件暂时无法核实。') from None

    metrics = []
    for index, (field, label, unit) in enumerate(_FIELDS):
        value = values.get(field)
        if type(value) is not int or not 0 <= value < 2**63:
            continue
        metric = {'label': label, 'value': str(value), 'unit': unit}
        if index > 1 and metrics:
            metric.update(detail=True, group='会话明细')
        metrics.append(metric)
    if not any(metric['value'] != '0' for metric in metrics):
        raise ReadFailure('这段 Hermes 会话尚无已记录的主用量。', 'unavailable')
    message = '部分字段尚无可用记录。' if len(metrics) < len(_FIELDS) else ''
    return metrics, message


def context(manager):
    """Select the newest created published chat with a verified native binding.

    Catalogue selection and file metadata checks do not open Hermes state.db.
    The caller invokes read only on an explicit refresh.
    """
    if manager is None:
        return _blocked('needs_configuration', '请先连接本机 Hermes。')
    if not os.access(_SANDBOX, os.X_OK):
        return _blocked('unavailable', '当前环境缺少 Hermes 记录的只读保护。')
    try:
        _catalogue_current(manager)
        chats = manager.catalogue.list()
        _catalogue_current(manager)
    except (OSError, ValueError, sqlite3.Error):
        return _blocked('unavailable', 'Hermes 会话目录暂时无法核实。')
    for chat in chats:
        identity = chat['id']
        try:
            binding = manager.store.current_binding(identity)
            if not binding or not binding['session_key'] or not binding['profile_home']:
                continue
            session_id = validate_session_id(binding['session_key'])
            with _profile_directory(binding) as (home, descriptor), FilePin(home / 'state.db') as record:
                if _file_state(descriptor)[:2] != record.current_identity:
                    continue
                record_anchor = record.anchor
        except (OSError, ValueError, sqlite3.Error):
            continue
        revision = _fingerprint('hermes-session-v1', identity, session_id, str(home),
                                binding['home_device'], binding['home_inode'],
                                binding['cwd'], binding['cwd_device'], binding['cwd_inode'], record_anchor)
        scope = f'会话 · {chat["title"]}（{identity[:8]}） · 已记录主用量'
        return SourceContext(fingerprint=revision, scope=scope,
                             read=lambda: _read(manager, identity, binding, record_anchor))
    return _blocked('unavailable', '还没有可核实原生记录的 Hermes 会话。')
