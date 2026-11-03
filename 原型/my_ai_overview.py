"""The home page reads saved metadata and existing caches, never discovery.

This module owns no storage, process or transport. A cache age describes the
last directory read; snapshot_at dates this projection, not an Agent event.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
import time

from agent_registry import IDS, LABELS
from model_catalog import PUBLIC_FIELDS
import native_work_status

PATH = '/api/management/my-ai'
_NATIVE = {'codex': 'native_chats', 'pi': 'pi_native_chats', 'hermes': 'hermes_native_chats',
           'claude': 'claude_native_chats', 'dsh': 'dsh_native_chats'}
_AGENT_FIELDS = ('id', 'name', 'device_id', 'available', 'model', 'provider', 'reason',
                 'connection', 'connection_label', 'check_scope', 'native_enabled', 'checked', 'has_saved_configuration')
_RECENT_FIELDS = ('key', 'kind', 'id', 'agent', 'session_id', 'title', 'timestamp',
                  'created_at', 'timestamp_kind', 'message_count', 'source_agents',
                  'available', 'archived', 'running', 'index_source', 'index_sources')
_AI_FIELDS = ('id', 'revision', 'name', 'agent_instance_id', 'device_id', 'model_id',
              'reasoning_effort')
_MEMORY_FIELDS = ('id', 'revision', 'name', 'program', 'connection_id', 'mode', 'source_kind',
                  'locked_mode')


def _age(stamp):
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return None
    age = time.monotonic() - stamp
    return round(age, 3) if math.isfinite(age) and age >= 0 else None


def _cached_models(service, issues):
    catalog = getattr(service, 'model_catalog', None)
    if catalog is None:
        return [], None
    lock = getattr(catalog, '_lock', None)
    if lock is None or not lock.acquire(blocking=False):
        issues.append('model_cache_busy')
        return [], None
    try:
        source = getattr(catalog, '_items', None)
        age = _age(getattr(catalog, '_checked_at', None))
        if not isinstance(source, list):
            return [], age
        rows, seen = [], set()
        for row in source[:6400]:
            if (not isinstance(row, dict) or row.get('selectable') is False
                    or row.get('agent_id') not in IDS or not isinstance(row.get('id'), str)
                    or row['id'] in seen):
                continue
            seen.add(row['id'])
            rows.append({key: deepcopy(row.get(key)) for key in PUBLIC_FIELDS})
        return rows, age
    finally:
        lock.release()


def _agents(service, profiles):
    cached = getattr(service, 'backend_cache', None)
    rows = {row['id']: row for row in cached or ()
            if isinstance(row, dict) and row.get('id') in IDS}
    native = {agent for agent, attr in _NATIVE.items() if getattr(service, attr, None) is not None}
    saved = {row.get('agent_instance_id', '')[6:] for row in profiles
             if isinstance(row.get('agent_instance_id'), str) and row['agent_instance_id'].startswith('local/')}
    registered = set(getattr(service, 'adapters', {})) | native | saved | set(rows)
    result = []
    for agent in IDS:
        if agent not in registered:
            continue
        source = rows.get(agent)
        checked = source is not None
        source = source or {}
        row = {'id': agent, 'name': LABELS[agent], 'device_id': 'local',
               'available': source.get('available') is True,
               'model': source.get('model') if isinstance(source.get('model'), str) else None,
               'provider': source.get('provider') if isinstance(source.get('provider'), str) else None,
               'reason': source.get('reason') if isinstance(source.get('reason'), str) else '尚未手动核对本机连接。',
               'connection': source.get('connection') if source.get('connection') in ('local', 'online') else 'unknown',
               'connection_label': '已保存的本机连接',
               'check_scope': '只读取已保存配置和已有检查缓存；未发送模型请求。',
               'native_enabled': agent in native, 'checked': checked,
               'has_saved_configuration': agent in native or agent in saved}
        result.append({key: row[key] for key in _AGENT_FIELDS})
    return result


def snapshot(service):
    """Read the already-owned service's metadata, without calling refresh APIs."""
    issues = []
    try:
        state = service.profiles.saved_state()
        ai = [{key: deepcopy(row[key]) for key in _AI_FIELDS if key in row}
              for row in state['profiles']]
    except Exception:
        ai = []
        issues.append('saved_ai_profiles_unavailable')
    try:
        # Saved parameters only: list() does not access a Bank or its content.
        source = service.memory_profiles.list()['profiles']
        memories = [{key: deepcopy(row[key]) for key in _MEMORY_FIELDS if key in row}
                    for row in source]
    except Exception:
        memories = []
        issues.append('saved_memory_profiles_unavailable')
    models, model_age = _cached_models(service, issues)
    try:
        capabilities = deepcopy(service._native_profile_capabilities())
    except Exception:
        capabilities = {}
        issues.append('native_catalogue_cache_unavailable')
    try:
        source = service.library.list(query='', agent='all', offset=0, limit=12, include_internal=False)
        recent = {key: deepcopy(source.get(key)) for key in ('total', 'next_offset', 'revision')}
        recent['items'] = [{key: deepcopy(row[key]) for key in _RECENT_FIELDS if key in row}
                           for row in source['items']]
    except Exception:
        recent = {'items': [], 'total': None, 'next_offset': None, 'revision': None}
        issues.append('recent_work_unavailable')
    try:
        work = native_work_status.snapshot(service)
    except Exception:
        work = None
        issues.append('work_cache_unavailable')
    return {'schema_version': 1, 'workspace_id': service.workspace_id,
            'scope': 'saved_and_cached_overview',
            'snapshot_at': datetime.now(timezone.utc).isoformat(),
            'connection_checked': False, 'memory_read': False, 'inference_performed': False,
            'management': {'workspace_id': service.workspace_id, 'agents': _agents(service, ai),
                           'models': models, 'native_profile_capabilities': capabilities,
                           'notice': '已保存参数与已有目录缓存；打开首页不发起新检查。'},
            'ai_profiles': ai, 'memory_profiles': memories, 'recent': recent, 'work_status': work,
            'cache': {'agent_checked_age_seconds': _age(getattr(service, 'backend_checked_at', None)),
                      'model_checked_age_seconds': model_age}, 'issues': issues}
