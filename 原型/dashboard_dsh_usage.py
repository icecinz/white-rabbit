"""Read the provider-reported usage of one exact, indexed DSH session."""

from contextlib import closing
import os
import sqlite3
import stat

from dashboard_usage import ReadFailure, SourceContext, _fingerprint
from dsh_history import MAX_FILE_BYTES, _sig
from dsh_history_adoption import _only_log
from rabbit_runtime.dsh.connection import validate_session_id
from rabbit_runtime.native_session_store import file_identity, open_directory, validate_path


_SCOPE = '单个 DSH 会话 · 本机保存记录'
_COUNTERS = (
    ('uncachedInputTokens', '未缓存输入'),
    ('outputTokens', '输出'),
    ('cacheReadTokens', '缓存读取'),
    ('cacheWriteTokens', '缓存写入'),
)


def _log_stat(parent, name):
    _only_log(parent, name)
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES):
        raise ValueError('Unsafe DSH log.')
    return info


def _read(reader, path, session_id, cwd, parent_identity, log_identity):
    try:
        parent = open_directory(path.parent)
        try:
            info = _log_stat(parent, path.name)
            if file_identity(os.fstat(parent)) != parent_identity or file_identity(info) != log_identity:
                raise ValueError('DSH log identity changed.')
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, 'rb') as stream:
                if _sig(os.fstat(stream.fileno())) != _sig(info):
                    raise ValueError('DSH log changed during open.')
                raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) != info.st_size:
                    raise ValueError('Incomplete DSH log.')
                result = reader(raw, compression='zstd' if path.name.endswith('.zstd') else 'none')
                if (_sig(os.fstat(stream.fileno())) != _sig(info)
                        or _sig(_log_stat(parent, path.name)) != _sig(info)):
                    raise ValueError('DSH log changed during read.')
        finally:
            os.close(parent)
        parent = open_directory(path.parent)
        try:
            if file_identity(os.fstat(parent)) != parent_identity or _sig(_log_stat(parent, path.name)) != _sig(info):
                raise ValueError('DSH log was replaced.')
        finally:
            os.close(parent)
        if (result['session_id'] != session_id or result['cwd'] != cwd
                or result['project_key'] != path.parent.parent.name or result['session_key'] != path.parent.name):
            raise ValueError('DSH log does not match its indexed session.')
    except (OSError, ValueError, RecursionError):
        raise ReadFailure('暂时无法核对 DSH 会话的原记录。') from None
    usage = result['usage']
    if usage is None:
        raise ReadFailure('这个会话的保存记录中还没有模型回报的 Token 用量。', 'unavailable')
    metrics = [dict(label='累计 Token', value=str(sum(usage.values())), unit='Token')]
    metrics.extend(dict(label=label, value=str(usage[key]), unit='Token', detail=True, group='会话明细')
                   for key, label in _COUNTERS)
    return metrics, ''


def context(history, manager):
    """Select only the newest eligible index entry and read its exact source on refresh."""
    if history is None or manager is None:
        return SourceContext(status='needs_configuration', message='当前入口还没有接入 DSH 保存记录。', scope=_SCOPE)
    decoder = manager.service.history_sources.dsh_decoder
    reader = getattr(decoder, 'inspect_usage', None)
    if not callable(reader):
        return SourceContext(status='unavailable', message='当前入口暂时无法读取 DSH 会话用量。', scope=_SCOPE)
    try:
        with closing(history._open()) as db:
            latest = db.execute("SELECT session_id FROM sessions WHERE agent='dsh' AND kind='root' "
                                'AND archived=0 AND parent_session_id IS NULL AND forked_from_id IS NULL '
                                'ORDER BY updated_at DESC, session_id DESC LIMIT 1').fetchone()
        if latest is None:
            return SourceContext(status='unavailable', message='还没有可核实的 DSH 保存会话。', scope=_SCOPE)
        session_id = validate_session_id(latest[0])
        adoption = manager.adoption
        source = adoption._indexed_source(session_id)
        path = validate_path(source['source_path'])
        cwd = str(validate_path(source['cwd']))
        if (path.name not in ('session.jsonl', 'session.jsonl.zstd') or path.parent.name != session_id
                or path.parent.parent.parent != adoption.root):
            raise ValueError('DSH log outside its indexed source root.')
        parent = open_directory(path.parent)
        try:
            parent_identity = file_identity(os.fstat(parent))
            log_identity = file_identity(_log_stat(parent, path.name))
        finally:
            os.close(parent)
        title = ''.join(character for character in source['title'] if character.isprintable()).strip()[:80]
        scope = f'{title} · 会话 …{session_id[-8:]} · 最近索引' if title else f'会话 …{session_id[-8:]} · 最近索引'
        fingerprint = _fingerprint('dsh:provider-reported-session:v1', session_id, cwd, str(path), parent_identity, log_identity)
        return SourceContext(fingerprint, lambda: _read(reader, path, session_id, cwd, parent_identity, log_identity), scope=scope)
    except (OSError, ValueError, sqlite3.Error, RecursionError):
        return SourceContext(status='unavailable', message='暂时无法核对 DSH 保存会话的原记录。', scope=_SCOPE)
