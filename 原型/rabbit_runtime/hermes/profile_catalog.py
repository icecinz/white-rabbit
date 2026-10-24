"""Pure Hermes model.options/profile codecs; no Agent, auth, config or RPC imports.

Model references identify the *reported* provider slug and exact model string.
Hermes' reasoning vocabulary is an Agent request vocabulary, not a claim that a
provider forwards every level unchanged. Hermes keeps its own provider mapping.
The installed native parser contract is checked separately by offline tests.
"""
import hashlib
import json
import re
import unicodedata


MAX_PROVIDERS = 256
MAX_MODELS = 4096
MAX_PROVIDER_CHARS = 256
MAX_MODEL_CHARS = 1024
MAX_NAME_CHARS = 1539
NATIVE_PREFIX = 'local/hermes/native/'
NATIVE_REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
EFFORT_SOURCE = 'Hermes 原生 parse_reasoning_effort；原生请求强度，提供商可能映射。'
NATIVE_REASONING_CONTRACT = {
    'source': 'hermes_constants.parse_reasoning_effort',
    'schema_version': 1,
    'verified_native_version': '0.21.1',
}
_REFERENCE = re.compile(re.escape(NATIVE_PREFIX) + r'[0-9a-f]{64}\Z')
_CHAT_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_NATIVE_DASH_FLAG = re.compile(r'[\u2012\u2013\u2014\u2015](provider|global|session|refresh|once)')
_NATIVE_FLAGS = frozenset(('--provider', '--global', '--session', '--refresh', '--once'))
_PICKER_FIELDS = frozenset(('id', 'agent_id', 'provider', 'model', 'name',
    'supported_efforts', 'default_effort', 'explicit_effort_supported', 'effort_source'))
_PROFILE_FIELDS = ('id', 'revision', 'name', 'agent_instance_id', 'device_id',
                   'model_id', 'reasoning_effort')


class HermesProfileUnavailable(ValueError):
    """Bounded public explanation; never embed native rows or credentials."""


def _text(value, limit):
    return (type(value) is str and 1 <= len(value) <= limit and bool(value.strip())
        and not any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value))


def _bad_catalogue():
    raise HermesProfileUnavailable('Hermes 原生模型目录格式无效、身份重复或超出当前范围；未使用部分目录。')


def is_hermes_native_model_id(value):
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


def native_model_id(provider, model):
    """Case-sensitive, unmodified reported provider/model identity."""
    if not _text(provider, MAX_PROVIDER_CHARS) or not _text(model, MAX_MODEL_CHARS):
        _bad_catalogue()
    canonical = json.dumps({'provider': provider, 'model': model}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    return NATIVE_PREFIX + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _picker_row(provider, model, name):
    return {'id': native_model_id(provider, model), 'agent_id': 'hermes',
        'provider': provider, 'model': model, 'name': name,
        'supported_efforts': list(NATIVE_REASONING_EFFORTS), 'default_effort': None,
        'explicit_effort_supported': True, 'effort_source': EFFORT_SOURCE}


def project_hermes_catalogue(data):
    """Whitelist model.options rows without endpoints, auth hints or free text.

    ``slug`` is the native picker request identity, including bare config keys
    and custom:* spellings. GUI aliases are neither execution identities nor
    projected fields: native resolution has its own precedence, so an alias
    overlapping another provider's slug is not an ambiguous selection here.
    Different providers may expose the same exact model id.
    The native catalogue can be curated or cached: this does not prove access,
    enumerate every model the account owns, or promise offline discovery.
    """
    if (type(data) is not dict or type(data.get('providers')) is not list
            or len(data['providers']) > MAX_PROVIDERS):
        _bad_catalogue()
    result, seen, provider_names = [], set(), {}
    for provider_row in data['providers']:
        if (type(provider_row) is not dict
                or not _text(provider_row.get('slug'), MAX_PROVIDER_CHARS)
                or not _text(provider_row.get('name'), 512)
                or type(provider_row.get('models')) is not list
                or type(provider_row.get('is_user_defined', False)) is not bool):
            _bad_catalogue()
        provider = provider_row['slug']
        # Hermes resolves provider spellings case-insensitively. Preserve the
        # spelling but require the selection slugs themselves to be unique.
        # Aliases can intentionally overlap, including on empty setup rows.
        name = provider.strip().lower()
        if name in provider_names:
            _bad_catalogue()
        provider_names[name] = provider
        for model in provider_row['models']:
            if not _text(model, MAX_MODEL_CHARS):
                _bad_catalogue()
            identity = native_model_id(provider, model)
            if identity in seen or len(result) >= MAX_MODELS:
                _bad_catalogue()
            seen.add(identity)
            result.append(_picker_row(provider, model, provider_row['name'] + ' · ' + model))
    return {'models': result}


def _projected_rows(catalogue):
    if (type(catalogue) is not dict or set(catalogue) != {'models'}
            or type(catalogue['models']) is not list or len(catalogue['models']) > MAX_MODELS):
        _bad_catalogue()
    rows, seen = [], set()
    for value in catalogue['models']:
        if (type(value) is not dict or set(value) != _PICKER_FIELDS
                or not _text(value.get('name'), MAX_NAME_CHARS)
                or type(value.get('explicit_effort_supported')) is not bool):
            _bad_catalogue()
        row = _picker_row(value.get('provider'), value.get('model'), value['name'])
        if row != value or row['id'] in seen:
            _bad_catalogue()
        seen.add(row['id']); rows.append(row)
    return rows


def validate_runtime_options(options):
    """Only literal provider/model plus an optional native request effort.

    Provider/model must round-trip the original /model flag parser in
    ``--session --provider <provider> <model>``. Shell quotes have no meaning
    there. Literal single spaces inside a model remain valid; normalization or
    flag interpretation is refused. This does not prove credential resolution
    or model normalization: the holder must verify native observed state.
    """
    if options is None:
        return {}
    if (type(options) is not dict or options and
            (not {'provider', 'model'} <= options.keys()
             or options.keys() - {'provider', 'model', 'reasoning_effort'})):
        raise HermesProfileUnavailable('Hermes 原生配置只接受成对的 provider/model 和可选原生请求强度。')
    if not options:
        return {}
    provider, model = options['provider'], options['model']
    if (not _text(provider, MAX_PROVIDER_CHARS) or not _text(model, MAX_MODEL_CHARS)
            or provider != provider.strip() or model != model.strip()
            or len(provider.split()) != 1 or provider != ' '.join(provider.split())
            or model != ' '.join(model.split())
            or any(token in _NATIVE_FLAGS for token in model.split())
            or _NATIVE_DASH_FLAG.search(provider) or _NATIVE_DASH_FLAG.search(model)):
        raise HermesProfileUnavailable('这个 Hermes 原生模型暂不能精确选择；未改名或替换模型。')
    result = {'provider': provider, 'model': model}
    if 'reasoning_effort' in options:
        effort = options['reasoning_effort']
        if type(effort) is not str or effort not in NATIVE_REASONING_EFFORTS:
            raise HermesProfileUnavailable('Hermes 原生请求强度不在已核验的原生输入范围。')
        result['reasoning_effort'] = effort
    return result


def resolve_hermes_profile(profile, catalogue, *, current=None):
    """Resolve a reference or a native request-strength-only configuration.

    Empty model keeps the original current model regardless of directory
    membership or picker alias. Management may validate its native effort with
    no live current model and receives only the reasoning_effort override.
    """
    if (type(profile) is not dict or profile.get('agent_instance_id') != 'local/hermes'
            or profile.get('device_id') != 'local'):
        raise HermesProfileUnavailable('这份配置不属于本机原生 Hermes。')
    if 'reasoning_effort' not in profile:
        raise HermesProfileUnavailable('Hermes 原生配置缺少请求强度选择。')
    effort = profile['reasoning_effort']
    if effort is not None and (type(effort) is not str or effort not in NATIVE_REASONING_EFFORTS):
        raise HermesProfileUnavailable('Hermes 原生请求强度不在已核验的原生输入范围。')
    reference = profile.get('model_id')
    if reference == '':
        if effort is None:
            return {}
        if current is None:
            return {'reasoning_effort': effort}
        if (type(current) is not dict
                or not _text(current.get('provider'), MAX_PROVIDER_CHARS)
                or not _text(current.get('model'), MAX_MODEL_CHARS)
                or any(current[key] != current[key].strip() for key in ('provider', 'model'))):
            raise HermesProfileUnavailable('Hermes 原生当前提供商与模型尚未核对，未猜测目录别名。')
        return {'provider': current['provider'], 'model': current['model'], 'reasoning_effort': effort}
    if not is_hermes_native_model_id(reference):
        raise HermesProfileUnavailable('这份配置不是 Hermes 原生目录引用；不能借用其他连接的模型身份。')
    rows = [row for row in _projected_rows(catalogue) if row['id'] == reference]
    if len(rows) != 1:
        raise HermesProfileUnavailable('当前 Hermes 原生目录没有这对提供商与模型，请刷新或编辑配置。')
    options = {key: rows[0][key] for key in ('provider', 'model')}
    if effort is not None:
        options['reasoning_effort'] = effort
    return validate_runtime_options(options)


def project_hermes_profiles(profiles, catalogue, *, current=None):
    if type(profiles) is not list:
        raise HermesProfileUnavailable('Hermes 原生配置列表格式无效。')
    result = []
    for profile in profiles:
        if type(profile) is not dict:
            raise HermesProfileUnavailable('Hermes 原生配置列表格式无效。')
        if profile.get('agent_instance_id') != 'local/hermes' or profile.get('device_id') != 'local':
            continue
        try:
            row = {key: profile[key] for key in _PROFILE_FIELDS}
        except KeyError:
            raise HermesProfileUnavailable('Hermes 原生配置字段不完整。') from None
        try:
            resolve_hermes_profile(profile, catalogue, current=current)
        except HermesProfileUnavailable as error:
            row.update(available=False, unavailable_reason=str(error))
        else:
            row.update(available=True, unavailable_reason=None)
        result.append(row)
    return result


def project_hermes_capabilities(snapshot, *, enabled):
    if type(enabled) is not bool:
        raise HermesProfileUnavailable('Hermes 原生入口状态无效。')
    result = {'enabled': enabled, 'source': 'hermes/model.options', 'catalogue_available': False,
        'observed_at': None, 'source_chat_id': None, 'models': []}
    if not enabled or snapshot is None:
        return result
    if (type(snapshot) is not dict or not _text(snapshot.get('observed_at'), 80)
            or type(snapshot.get('source_chat_id')) is not str
            or _CHAT_ID.fullmatch(snapshot['source_chat_id']) is None):
        _bad_catalogue()
    rows = _projected_rows(snapshot.get('catalogue'))
    result.update(catalogue_available=True, observed_at=snapshot['observed_at'],
        source_chat_id=snapshot['source_chat_id'])
    for row in rows:
        try:
            validate_runtime_options({key: row[key] for key in ('provider', 'model')})
        except HermesProfileUnavailable:
            continue
        result['models'].append(row)
    return result
