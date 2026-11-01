"""Bounded DSH model-directory projection for display, never model selection.

``session.models`` is advisory and may contain provider-local failures.  Keep
only model identities, names and original effort IDs; descriptions, failures,
current settings and any additional native fields do not leave this boundary.
"""
import json
import re
import unicodedata


MAX_PROVIDERS = 256
MAX_MODELS = 4096
MAX_EFFORTS = 32
MAX_BYTES = 512 * 1024
_FIELDS = frozenset(('provider', 'provider_name', 'model', 'name', 'efforts'))
_PRIVATE = re.compile(r'(?:/(?:Users|home|private|tmp)/|[A-Za-z]:\\|\b(?:sk[-_]|deck_)[A-Za-z0-9_-]{12})')


def _invalid():
    raise ValueError('DSH 模型目录格式无效、含不安全信息或超出读取范围。')


def _text(value, limit):
    return (type(value) is str and bool(value.strip()) and len(value) <= limit
            and not any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)
            and not _PRIVATE.search(value))


def validate_projection(value):
    """Validate and detach a guide-only projection, including saved records."""
    if (type(value) is not dict or set(value) != {'models', 'partial'}
            or type(value['partial']) is not bool or type(value['models']) is not list
            or len(value['models']) > MAX_MODELS):
        _invalid()
    result, seen, provider_names = [], set(), {}
    for item in value['models']:
        if (type(item) is not dict or set(item) != _FIELDS
                or not _text(item['provider'], 256) or not _text(item['provider_name'], 512)
                or not _text(item['model'], 1024) or not _text(item['name'], 512)):
            _invalid()
        provider, model, efforts = item['provider'], item['model'], item['efforts']
        if ((provider, model) in seen or provider in provider_names and provider_names[provider] != item['provider_name']
                or len(provider_names) >= MAX_PROVIDERS and provider not in provider_names):
            _invalid()
        if efforts is not None and (type(efforts) is not list or not efforts or len(efforts) > MAX_EFFORTS
                or any(not _text(level, 128) for level in efforts) or len(set(efforts)) != len(efforts)):
            _invalid()
        seen.add((provider, model)); provider_names[provider] = item['provider_name']
        result.append({key: (list(efforts) if key == 'efforts' and efforts is not None else item[key])
                       for key in ('provider', 'provider_name', 'model', 'name', 'efforts')})
    projection = {'models': result, 'partial': value['partial']}
    if len(json.dumps(projection, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_BYTES:
        _invalid()
    return projection


def project(value):
    """Project one original ``session.models`` result, without calling DSH."""
    if (type(value) is not dict or type(value.get('current')) is not dict
            or type(value.get('routable')) is not bool or type(value.get('groups')) is not list
            or type(value.get('failures')) is not list or len(value['groups']) > MAX_PROVIDERS
            or len(value['failures']) > MAX_PROVIDERS
            or any(type(failure) is not dict for failure in value['failures'])):
        _invalid()
    result, providers = [], set()
    for group in value['groups']:
        if (type(group) is not dict or not _text(group.get('id'), 256)
                or not _text(group.get('name'), 512) or type(group.get('models')) is not list
                or group['id'] in providers):
            _invalid()
        providers.add(group['id'])
        for model in group['models']:
            if type(model) is not dict or not _text(model.get('id'), 1024) or not _text(model.get('name'), 512):
                _invalid()
            reasoning = model.get('reasoning')
            if 'reasoning' not in model:
                efforts = None
            else:
                if (type(reasoning) is not dict or type(reasoning.get('efforts')) is not list
                        or not reasoning['efforts'] or len(reasoning['efforts']) > MAX_EFFORTS):
                    _invalid()
                efforts = []
                for entry in reasoning['efforts']:
                    if type(entry) is not dict or not _text(entry.get('id'), 128):
                        _invalid()
                    efforts.append(entry['id'])
            if len(result) >= MAX_MODELS:
                _invalid()
            result.append({'provider': group['id'], 'provider_name': group['name'],
                           'model': model['id'], 'name': model['name'], 'efforts': efforts})
    return validate_projection({'models': result, 'partial': bool(value['failures'])})
