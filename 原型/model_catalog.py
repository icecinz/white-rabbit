"""Bounded model discovery and credential-free, immutable per-turn routing.

Catalog entries describe candidates. Connector readiness never claims that an
account is entitled to inference on every candidate. Adapters own discovery and
recheck the accepted route before inference; credentials never enter this API.
"""
import copy
import hashlib
import inspect
import json
import re
import threading
import time

from profile_capabilities import public_effort_capability, validate_profile_effort
from agent_registry import IDS as BACKENDS


PUBLIC_FIELDS = ('id', 'model', 'provider', 'name', 'agent_id', 'available',
                 'status', 'source', 'is_default', 'detail', 'image_input', 'image_input_source',
                 'file_output', 'file_output_source',
                 'supported_efforts', 'default_effort', 'explicit_effort_supported', 'effort_source')
EFFORT_PUBLIC_FIELDS = ('supported_efforts', 'default_effort', 'explicit_effort_supported', 'effort_source')
MODEL_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,159}(?:\[1m\])?$')
ROUTE_FIELDS = ('backend', 'model_id', 'model', 'provider', 'base_url', 'default_model',
                'configured_provider', 'binary', 'route_fingerprint', 'reasoning_effort',
                'catalog_model', 'name', 'source', 'image_input', 'image_input_source',
                'file_output', 'file_output_source', 'effort_selection', 'api_mode')
CAPABILITY_STATES = ('supported', 'unsupported', 'unknown')


def image_capability(value='unknown', source='未确认图像输入能力'):
    return {'image_input': value if value in CAPABILITY_STATES else 'unknown',
            'image_input_source': source if isinstance(source, str) and len(source) <= 512
                                  else '未确认图像输入能力'}


def file_capability(value='unknown', source='未确认文件输出能力'):
    return {'file_output': value if value in CAPABILITY_STATES else 'unknown',
            'file_output_source': source if isinstance(source, str) and len(source) <= 512
                                   else '未确认文件输出能力'}


def clean_route(route):
    if not isinstance(route, dict):
        return {}
    result = {key: value for key, value in route.items()
              if key in ROUTE_FIELDS and isinstance(value, str) and len(value) <= 2048}
    result.update(image_capability(result.get('image_input'), result.get('image_input_source')))
    result.update(file_capability(result.get('file_output'), result.get('file_output_source')))
    return result


def valid_model(value):
    return isinstance(value, str) and MODEL_PATTERN.fullmatch(value) is not None


def identifier(backend, model):
    if backend not in BACKENDS or not valid_model(model):
        raise ValueError('模型标识无效。')
    return 'local/' + backend + '/' + model


def fingerprint(values):
    """Hash routing settings only. Never supply keys, tokens or account IDs."""
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def entry(backend, model, state, *, name=None, source='本机默认配置',
          status='候选', detail='', is_default=False, route=None, selectable=True,
          image_input='unknown', image_input_source='未确认图像输入能力', effort_capability=None,
          file_output='unknown', file_output_source='未确认文件输出能力',
          model_key=None):
    key = model if model_key is None else model_key
    return {'id': identifier(backend, key), 'model': model, 'model_key': key,
            'provider': str(state.get('provider') or ''), 'name': name or model,
            'agent_id': backend, 'available': bool(state.get('available')),
            'status': status, 'source': source, 'is_default': bool(is_default),
            'detail': detail, 'route': route or {}, 'selectable': bool(selectable),
            **image_capability(image_input, image_input_source),
            **file_capability(file_output, file_output_source),
            **public_effort_capability(backend, effort_capability)}


class ModelCatalog:
    def __init__(self, adapters, ttl=60):
        self.adapters = adapters
        self.ttl = ttl
        self._items = None
        self._checked_at = 0
        self._lock = threading.RLock()

    def _refresh(self):
        items = []
        for backend in BACKENDS:
            adapter = self.adapters.get(backend)
            if adapter is None:
                continue
            try:
                if callable(getattr(adapter, 'model_catalog', None)):
                    candidates = adapter.model_catalog()
                else:
                    # Existing synthetic adapters and older connectors may only
                    # expose their default. Do not invent other model choices.
                    state = adapter.available()
                    model = state.get('model')
                    candidates = [entry(backend, model, state, is_default=True)] if valid_model(model) else []
                seen = set()
                for candidate in candidates[:200]:
                    if not isinstance(candidate, dict):
                        continue
                    model = candidate.get('model')
                    model_key = candidate.get('model_key', model)
                    if not valid_model(model) or not valid_model(model_key):
                        continue
                    expected = identifier(backend, model_key)
                    if candidate.get('id') != expected or candidate.get('agent_id') != backend or expected in seen:
                        continue
                    seen.add(expected)
                    # New effort fields are bounded before copying. A malformed
                    # connector array/diagnostic cannot expand this projection.
                    public = {key: copy.deepcopy(candidate.get(key)) for key in PUBLIC_FIELDS
                              if key not in EFFORT_PUBLIC_FIELDS}
                    public['available'] = bool(public['available'])
                    public['is_default'] = bool(public['is_default'])
                    public.update(image_capability(public['image_input'], public['image_input_source']))
                    public.update(file_capability(public['file_output'], public['file_output_source']))
                    public.update(public_effort_capability(backend, candidate))
                    route = clean_route(candidate.get('route') or {})
                    route.update(backend=backend, model_id=expected, model=model,
                                 provider=public['provider'], name=public['name'], source=public['source'])
                    route.update({key: public[key] for key in ('image_input', 'image_input_source',
                                                             'file_output', 'file_output_source')})
                    route.setdefault('base_url', '')
                    items.append(dict(public, model_key=model_key, route=route,
                                      selectable=candidate.get('selectable') is not False))
            except Exception:
                # Adapter diagnostics can include environment values. A failed
                # connector contributes no fabricated options or raw errors.
                continue
        self._items = items
        self._checked_at = time.monotonic()

    def models(self, fresh=False):
        with self._lock:
            if self._items is None or fresh or time.monotonic() - self._checked_at >= self.ttl:
                self._refresh()
            return [{key: copy.deepcopy(item[key]) for key in PUBLIC_FIELDS}
                    for item in self._items if item['selectable']]

    def resolve(self, backend, model_id='', *, reasoning_effort=None):
        if backend not in BACKENDS or backend not in self.adapters:
            raise ValueError('请选择已接入的 Agent。')
        if not isinstance(model_id, str) or len(model_id) > 180:
            raise ValueError('模型选择无效，请重新选择。')
        with self._lock:
            self.models()
            candidates = [item for item in self._items if item['agent_id'] == backend]
            selected = (next((item for item in candidates if item['id'] == model_id and item['selectable']), None)
                        if model_id else next((item for item in candidates if item['is_default']), None))
            if selected is None:
                raise ValueError('这位 Agent 的模型目录里没有该选项，请刷新后重新选择。')
            if not selected['available']:
                raise ValueError('这位 Agent 的本机连接暂不可用，请检查管理页面。')
            route = copy.deepcopy(selected['route'])
            capture = getattr(self.adapters[backend], 'capture_route', None)
            explicit_effort = reasoning_effort is not None
            if explicit_effort:
                if backend == 'hermes' or model_id:
                    validate_profile_effort(public_effort_capability(backend, selected), reasoning_effort)
                if not callable(capture):
                    raise ValueError('这个接入器尚未支持显式强度，不能忽略选择继续发送。')
                try:
                    parameters = inspect.signature(capture).parameters.values()
                except (TypeError, ValueError):
                    raise ValueError('这个接入器尚未支持显式强度，不能忽略选择继续发送。') from None
                if not any((item.name == 'reasoning_effort' and item.kind in
                            (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY))
                           or item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
                    raise ValueError('这个接入器尚未支持显式强度，不能忽略选择继续发送。')
            if callable(capture):
                selection = selected['model_key'] if model_id else None
                captured = (capture(selection, reasoning_effort=reasoning_effort)
                            if explicit_effort else capture(selection))
                if explicit_effort:
                    validate_profile_effort(public_effort_capability(backend, captured), reasoning_effort)
                    if (not isinstance(captured, dict) or captured.get('effort_selection') != 'explicit'
                            or captured.get('reasoning_effort') != reasoning_effort):
                        raise ValueError('接入器没有保留这次强度选择，请刷新后重新发送。')
                current = clean_route(captured)
                if current.get('provider') != selected['provider']:
                    raise ValueError('模型目录对应的本机提供商已变化，请刷新后重新选择。')
                if model_id:
                    # An explicit candidate belongs to the connector/address
                    # that supplied its directory. Defaults may follow current
                    # configuration, but stale explicit options cannot silently
                    # move to a different endpoint or executable.
                    for field in ('base_url', 'configured_provider', 'binary', 'route_fingerprint', 'api_mode'):
                        expected = selected['route'].get(field)
                        if isinstance(expected, str) and expected and current.get(field) != expected:
                            raise ValueError('模型目录对应的本机连接已变化，请刷新后重新选择。')
                route.update(current)
                if model_id and current.get('model') != selected['model']:
                    raise ValueError('这轮模型选择已变化，请重新选择。')
                if model_id and current.get('model_id') != selected['id']:
                    raise ValueError('这轮模型身份已变化，请重新选择。')
                route['model_id'] = selected['id'] if model_id else identifier(backend, route['model'])
                if not model_id and route['model'] != selected['model']:
                    route['name'] = route['model']
                    route['source'] = current.get('source') or '本机默认配置'
                    route.pop('catalog_model', None)
            route['effort_selection'] = 'explicit' if explicit_effort else 'agent_default'
            return route

    def validate(self, route):
        if not isinstance(route, dict) or route.get('backend') not in self.adapters:
            raise ValueError('这轮回答的模型路线不完整。')
        if route.get('effort_selection', 'agent_default') not in ('explicit', 'agent_default'):
            raise ValueError('这轮回答的强度选择方式无效。')
        if route.get('effort_selection') == 'explicit' and route['backend'] != 'codex':
            raise ValueError('这位 Agent 尚未支持显式强度。')
        adapter = self.adapters[route['backend']]
        check = getattr(adapter, 'validate_route', None)
        return check(route) if callable(check) else copy.deepcopy(route)
