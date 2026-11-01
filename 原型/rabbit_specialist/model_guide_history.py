"""Past model observations for the guide only, never for native validation."""
from datetime import datetime, timezone
import json
import re
import uuid
from rabbit_runtime.dsh.model_catalog import validate_projection


PATH = '/api/management/model-guide-history'
SOURCES = {'codex': 'codex/model/list', 'pi': 'pi/get_available_models',
           'hermes': 'hermes/model.options', 'claude': 'claude/sdk-initialize',
           'dsh': 'dsh/session.models'}
EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'))
_PRIVATE = re.compile(r'(?:/(?:Users|home|private|tmp)/|[A-Za-z]:\\|\b(?:sk[-_]|deck_)[A-Za-z0-9_-]{12})')
_DDL = '''CREATE TABLE IF NOT EXISTS model_guide_history (
    workspace_id TEXT NOT NULL, agent TEXT NOT NULL, observed_us INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL, PRIMARY KEY (workspace_id, agent))'''


def _time(value):
    if type(value) is not str or len(value) > 100:
        raise ValueError('模型目录观察时间无效。')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError()
        delta = date.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds)
    except (ValueError, OverflowError):
        raise ValueError('模型目录观察时间无效。') from None


def _identity(value):
    if value is None:
        return None
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('模型目录来源无效。') from None
    return value


def observations(value, workspace):
    if (type(value) is not dict or set(value) != {'workspace_id', 'observations'}
            or value['workspace_id'] != workspace or type(value['observations']) is not list
            or len(value['observations']) > len(SOURCES)):
        raise ValueError('模型目录检查范围无效。')
    seen = set()
    for item in value['observations']:
        if (type(item) is not dict or set(item) != {'agent', 'observed_at', 'source_chat_id'}
                or type(item['agent']) is not str or item['agent'] not in SOURCES
                or item['agent'] in seen):
            raise ValueError('模型目录检查范围无效。')
        seen.add(item['agent'])
        _time(item['observed_at']); _identity(item['source_chat_id'])
        if item['agent'] != 'codex' and item['source_chat_id'] is None:
            raise ValueError('模型目录来源无效。')
    return value['observations']


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(ch) < 32 or ord(ch) == 127 or 0xd800 <= ord(ch) <= 0xdfff for ch in value)
            or _PRIVATE.search(value)):
        raise ValueError('模型目录包含无法保存的信息。')
    return value


def _snapshot(agent, source):
    if agent == 'dsh':
        if type(source) is not dict or source.get('source') != SOURCES['dsh']:
            raise ValueError('模型目录尚未核对。')
        observed_at = source.get('observed_at'); _time(observed_at)
        identity = _identity(source.get('source_chat_id'))
        if identity is None:
            raise ValueError('模型目录来源无效。')
        projection = validate_projection({'models': source.get('models'), 'partial': source.get('partial')})
        result = {'source': SOURCES['dsh'], 'observed_at': observed_at, 'source_chat_id': identity, **projection}
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(raw.encode('utf-8')) > 512 * 1024:
            raise ValueError('模型目录超过保存范围。')
        return result, raw
    if (type(source) is not dict or source.get('source') != SOURCES[agent]
            or source.get('enabled') is not True or source.get('catalogue_available') is not True
            or type(source.get('models')) is not list or len(source['models']) > 4096):
        raise ValueError('模型目录尚未核对。')
    observed_at = source.get('observed_at'); _time(observed_at)
    identity = _identity(source.get('source_chat_id'))
    if agent != 'codex' and identity is None:
        raise ValueError('模型目录来源无效。')
    models = []
    for row in source['models']:
        if type(row) is not dict or type(row.get('explicit_effort_supported')) is not bool:
            raise ValueError('模型目录格式无效。')
        levels = row.get('supported_efforts')
        if (type(levels) is not list or len(levels) > len(EFFORTS)
                or any(type(level) is not str or level not in EFFORTS for level in levels)
                or len(set(levels)) != len(levels)):
            raise ValueError('模型目录强度无效。')
        if row.get('agent_id') != agent:
            raise ValueError('模型目录 Agent 无效。')
        default = row.get('default_effort')
        if default is not None and (type(default) is not str or default not in EFFORTS):
            raise ValueError('模型目录默认强度无效。')
        item = {'id': _text(row.get('id'), 1536), 'agent_id': agent,
                'name': _text(row.get('name'), 2048), 'model': _text(row.get('model'), 1024),
                'supported_efforts': levels, 'default_effort': default,
                'explicit_effort_supported': row['explicit_effort_supported']}
        if 'provider' in row:
            item['provider'] = _text(row['provider'], 256)
        if agent in ('pi', 'hermes'):
            item['effort_source'] = _text(row.get('effort_source'), 160)
        if agent == 'pi':
            native_levels = row.get('native_thinking_levels')
            if (type(native_levels) is not list or len(native_levels) > len(EFFORTS)
                    or any(type(level) is not str or level not in EFFORTS | {'off'} for level in native_levels)):
                raise ValueError('模型目录原生强度无效。')
            item['native_thinking_levels'] = native_levels
        models.append(item)
    result = {'enabled': True, 'catalogue_available': True, 'source': SOURCES[agent], 'observed_at': observed_at,
              'source_chat_id': identity, 'models': models}
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(raw.encode('utf-8')) > 512 * 1024:
        raise ValueError('模型目录超过保存范围。')
    return result, raw


class ModelGuideHistory:
    def __init__(self, store, workspace_id):
        self.store, self.workspace_id = store, workspace_id

    def read(self):
        with self.store.lock, self.store._open(readonly=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_guide_history'").fetchone()
            rows = db.execute('SELECT agent, snapshot_json FROM model_guide_history WHERE workspace_id=?',
                              (self.workspace_id,)).fetchall() if exists else []
        result = {}
        for row in rows:
            try:
                agent = row['agent']
                if agent not in SOURCES or len(row['snapshot_json'].encode('utf-8')) > 512 * 1024:
                    continue
                source = json.loads(row['snapshot_json'])
                # Validate persisted data again; it is display-only and never
                # treated as a live native connection or execution capability.
                result[agent] = _snapshot(agent, dict(source, enabled=True, catalogue_available=True))[0]
            except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
                continue
        return {'workspace_id': self.workspace_id, 'catalogues': result}

    def save(self, requested, sources):
        selections = observations(requested, self.workspace_id)
        if type(sources) is not dict:
            raise ValueError('模型目录无法核对。')
        prepared, unchanged = [], []
        for item in selections:
            agent = item['agent']; source = sources.get(agent)
            if (type(source) is not dict or source.get('observed_at') != item['observed_at']
                    or source.get('source_chat_id') != item['source_chat_id']):
                unchanged.append(agent); continue
            try:
                snapshot, raw = _snapshot(agent, source)
                prepared.append((agent, _time(snapshot['observed_at']), raw))
            except (ValueError, TypeError, UnicodeError):
                unchanged.append(agent)
        saved = []
        if prepared:
            with self.store.lock, self.store._open() as db:
                db.execute(_DDL)
                for agent, observed_us, raw in prepared:
                    updated = db.execute('INSERT INTO model_guide_history (workspace_id,agent,observed_us,snapshot_json) '
                        'VALUES (?,?,?,?) ON CONFLICT(workspace_id,agent) DO UPDATE SET '
                        'observed_us=excluded.observed_us,snapshot_json=excluded.snapshot_json '
                        'WHERE model_guide_history.observed_us < excluded.observed_us',
                        (self.workspace_id, agent, observed_us, raw))
                    (saved if updated.rowcount else unchanged).append(agent)
        return {'workspace_id': self.workspace_id, 'saved': saved, 'unchanged': unchanged}
