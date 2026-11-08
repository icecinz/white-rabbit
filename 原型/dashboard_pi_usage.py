"""Read the saved branch of one exact Pi session, without running Pi."""

import sqlite3

from dashboard_usage import ReadFailure, SourceContext, _fingerprint
from rabbit_runtime.pi.session_file import session_usage_scope, validate_session_file


_SCOPE = '单个 Pi 会话 · 已保存分支'
_COUNTERS = (
    ('total', '累计 Token'),
    ('input', '输入'),
    ('output', '输出'),
    ('cacheRead', '缓存读取'),
    ('cacheWrite', '缓存写入'),
)


def _read(location, session_id, scope):
    try:
        usage = validate_session_file(**location, session_id=session_id, usage_scope=scope)['branch_usage']
    except ValueError:
        raise ReadFailure('Pi 保存记录或会话身份暂时无法核对，请稍后更新。') from None
    if usage is None:
        raise ReadFailure('这个保存分支暂时没有完整可核实的 Token 用量。', 'unavailable')
    return [dict(label=label, value=str(usage[key]), unit='Token', detail=key != 'total')
            for key, label in _COUNTERS], ''


def context(history, manager):
    """Choose an existing bound session, or the latest indexed original one."""
    if history is None and manager is None:
        return SourceContext(status='needs_configuration', message='当前入口还没有接入 Pi 保存记录。', scope=_SCOPE)
    try:
        binding = manager.store.latest_source_binding() if manager is not None else None
        if binding is not None:
            source = binding
            title = manager.catalogue.get(binding['chat_id'])['title']
        else:
            adoption = getattr(history, 'adoption', history)
            source = adoption.latest_usage_source() if adoption is not None else None
            if source is None:
                return SourceContext(status='unavailable', message='还没有可核实的 Pi 保存会话。', scope=_SCOPE)
            title = source['title']
        session_id = source['session_id']
        label = f'{title} · 会话 …{session_id[-8:]} · 已保存分支累计'
        location = {'path': source['session_file'], 'session_dir': source['session_dir'], 'cwd': source['cwd']}
        scope = session_usage_scope(**location)
        if binding is not None:
            directories = dict(scope[0])
            for role in ('cwd', 'session_dir'):
                if directories[binding[role]] != (binding[role + '_device'], binding[role + '_inode']):
                    raise ValueError('Pi binding directory changed')
        fingerprint = _fingerprint('pi:last-saved-ancestor-chain:v1', session_id, scope)
        return SourceContext(fingerprint, lambda: _read(location, session_id, scope), scope=label)
    except (OSError, ValueError, sqlite3.Error, RecursionError):
        return SourceContext(status='unavailable', message='暂时无法核对 Pi 保存会话的原记录。', scope=_SCOPE)
