"""Pure Pi directory/profile codecs; no RPC, storage or text-adapter routing.

The native reference reserves ``local/pi/native/<sha256>``. Its digest identifies
the exact provider/model pair; it is never decoded or treated as a model name.
Raw Pi data becomes a small picker projection, without headers or endpoints.
Startup syntax checks cannot prove the CLI selected the requested model: the
holder must independently compare get_state's exact provider and model id.
"""
import hashlib
import json
import re
import unicodedata


MAX_MODELS = 4096
MAX_PROVIDER_CHARS = 256
MAX_MODEL_CHARS = 1024
MAX_NAME_CHARS = 512
NATIVE_PREFIX = 'local/pi/native/'
_REFERENCE = re.compile(re.escape(NATIVE_PREFIX) + r'[0-9a-f]{64}\Z')
_CHAT_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_PICKER_FIELDS = frozenset(('id', 'agent_id', 'provider', 'model', 'name',
    'supported_efforts', 'default_effort', 'explicit_effort_supported',
    'native_thinking_levels', 'effort_source'))
_PROFILE_FIELDS = ('id', 'revision', 'name', 'agent_instance_id', 'device_id',
                   'model_id', 'reasoning_effort')
THINKING_LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh')
_VERIFIED_EFFORT_SOURCE = 'Pi 原生 getSupportedThinkingLevels；none 对应原生 off，不代表关闭厂商思考。'
_UNKNOWN_EFFORT_SOURCE = 'Pi 原生思考强度尚未核验。'
_MISSING = object()


class PiProfileUnavailable(ValueError):
    """A bounded explanation, never raw directory rows or credentials."""


def _text(value, limit):
    return (type(value) is str and 1 <= len(value) <= limit and bool(value.strip())
        and not any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value))


def _bad_catalogue():
    raise PiProfileUnavailable('Pi 原生模型目录格式无效、重复或超出当前范围；未使用部分目录。')


def is_pi_native_model_id(value):
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


def native_model_id(provider, model):
    """Stable, case-sensitive identity; never strip, normalize or guess aliases."""
    if not _text(provider, MAX_PROVIDER_CHARS) or not _text(model, MAX_MODEL_CHARS):
        _bad_catalogue()
    canonical = json.dumps({'provider': provider, 'model': model}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    return NATIVE_PREFIX + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _thinking_levels(value):
    if (type(value) is not list or len(value) > len(THINKING_LEVELS)
            or any(type(level) is not str or level not in THINKING_LEVELS for level in value)
            or len(set(value)) != len(value)):
        _bad_catalogue()
    return list(value)


def _input_modalities(value):
    if type(value) is not list or any(type(item) is not str or item not in ('text', 'image') for item in value):
        _bad_catalogue()
    return list(value)


def _picker_row(provider, model, name, levels=None, input_modalities=_MISSING):
    native_levels = [] if levels is None else _thinking_levels(levels)
    efforts = ['none' if level == 'off' else level for level in native_levels]
    row = {'id': native_model_id(provider, model), 'agent_id': 'pi', 'provider': provider,
        'model': model, 'name': name, 'supported_efforts': efforts, 'default_effort': None,
        'explicit_effort_supported': bool(efforts), 'native_thinking_levels': native_levels,
        'effort_source': _UNKNOWN_EFFORT_SOURCE if levels is None else _VERIFIED_EFFORT_SOURCE}
    if input_modalities is not _MISSING:
        row['input'] = _input_modalities(input_modalities)
    return row


def project_pi_catalogue(data, *, levels=None):
    """Project get_available_models' data.models, retaining all valid identities.

    No silent limit, dedupe or credential-bearing extra fields. Optional levels
    must be a complete provider/model result from the native SDK's pure helper,
    never inferred from a model's reasoning boolean. Native identifiers stay
    intact; editing candidates separately filter unrepresentable CLI arguments.
    """
    if type(data) is not dict or type(data.get('models')) is not list or len(data['models']) > MAX_MODELS:
        _bad_catalogue()
    level_rows = None
    if levels is not None:
        if type(levels) is not list or len(levels) != len(data['models']):
            _bad_catalogue()
        level_rows = {}
        for value in levels:
            if type(value) is not dict:
                _bad_catalogue()
            identity = native_model_id(value.get('provider'), value.get('model'))
            if identity in level_rows:
                _bad_catalogue()
            level_rows[identity] = _thinking_levels(value.get('thinking_levels'))
    rows, seen = [], set()
    for value in data['models']:
        if (type(value) is not dict or not _text(value.get('provider'), MAX_PROVIDER_CHARS)
                or not _text(value.get('id'), MAX_MODEL_CHARS)
                or not _text(value.get('name'), MAX_NAME_CHARS)):
            _bad_catalogue()
        identity = native_model_id(value['provider'], value['id'])
        if level_rows is not None and identity not in level_rows:
            _bad_catalogue()
        row = _picker_row(value['provider'], value['id'], value['name'],
                          None if level_rows is None else level_rows[identity], value.get('input', _MISSING))
        if row['id'] in seen:
            _bad_catalogue()
        seen.add(row['id']); rows.append(row)
    return {'models': rows}


def _projected_rows(catalogue):
    if (type(catalogue) is not dict or set(catalogue) != {'models'}
            or type(catalogue['models']) is not list or len(catalogue['models']) > MAX_MODELS):
        _bad_catalogue()
    rows, seen = [], set()
    for value in catalogue['models']:
        if (type(value) is not dict or not _PICKER_FIELDS <= value.keys() or value.keys() - (_PICKER_FIELDS | {'input'})
                or value.get('agent_id') != 'pi' or not _text(value.get('name'), MAX_NAME_CHARS)
                or type(value.get('supported_efforts')) is not list
                or type(value.get('explicit_effort_supported')) is not bool
                or value.get('default_effort') is not None
                or value.get('effort_source') not in (_VERIFIED_EFFORT_SOURCE, _UNKNOWN_EFFORT_SOURCE)):
            _bad_catalogue()
        native_levels = _thinking_levels(value.get('native_thinking_levels'))
        levels = native_levels if value['effort_source'] == _VERIFIED_EFFORT_SOURCE else None
        row = _picker_row(value.get('provider'), value.get('model'), value['name'], levels, value.get('input', _MISSING))
        if value != row or row['id'] in seen:
            _bad_catalogue()
        seen.add(row['id']); rows.append(row)
    return rows


def require_pi_image_support(catalogue, startup_options):
    """An exact frozen native target needs an explicit original image claim.

    No default is inferred from model names, reasoning or an omitted field.
    This pure gate neither selects a model nor proves a model call occurred.
    """
    options = validate_startup_options(startup_options)
    if not options:
        raise PiProfileUnavailable('Pi 原图输入需要先核对本轮冻结的提供商与模型。')
    matches = [row for row in _projected_rows(catalogue)
               if row['provider'] == options['provider'] and row['model'] == options['model']]
    if len(matches) != 1 or 'image' not in matches[0].get('input', []):
        raise PiProfileUnavailable('Pi 原生目录没有明确声明本轮模型支持原图；图片尚未发送。')


def validate_startup_options(options):
    """Only literal, paired provider/model arguments; None/{} inherit Pi settings.

    Pi checks exact model identities before colon/thinking and fuzzy parsing, so
    colons, slashes, Unicode and other native identity characters stay literal.
    No argument is stripped or renamed; controls, flag-shaped values and edge
    whitespace are refused. The holder must prove exact get_state identity
    before any prompt, rather than trusting a successful CLI startup.
    """
    if options is None:
        return {}
    if (type(options) is not dict or options and
            (not {'provider', 'model'} <= options.keys() or options.keys() - {'provider', 'model', 'thinking'})):
        raise PiProfileUnavailable('Pi 原生启动只接受成对的 provider/model 和可选原生 thinking。')
    if not options:
        return {}
    provider, model = options['provider'], options['model']
    if (not _text(provider, MAX_PROVIDER_CHARS) or not _text(model, MAX_MODEL_CHARS)
            or provider != provider.strip() or model != model.strip()
            or provider.startswith('-') or model.startswith('-')):
        raise PiProfileUnavailable('这个 Pi 原生模型的启动语法暂不支持精确选择；未改名或替换模型。')
    result = {'provider': provider, 'model': model}
    if 'thinking' in options:
        if type(options['thinking']) is not str or options['thinking'] not in THINKING_LEVELS:
            raise PiProfileUnavailable('Pi 原生 thinking 需要已核验的原生等级；不能猜测或降级。')
        result['thinking'] = options['thinking']
    return result


def resolve_pi_profile(profile, catalogue, *, current=None):
    """Resolve intent; current is a freshly verified Pi get_state.model object.

    Empty model with explicit effort pins that exact current provider/id. Empty
    model and None effort add no override; None effort never changes thinking.
    """
    if (type(profile) is not dict or profile.get('agent_instance_id') != 'local/pi'
            or profile.get('device_id') != 'local'):
        raise PiProfileUnavailable('这份配置不属于本机原生 Pi。')
    if 'reasoning_effort' not in profile:
        raise PiProfileUnavailable('Pi 原生配置缺少思考强度选择。')
    effort = profile['reasoning_effort']
    if effort is not None and type(effort) is not str:
        raise PiProfileUnavailable('Pi 原生思考强度选择无效。')
    model_id = profile.get('model_id')
    if model_id == '':
        if effort is None:
            return {}
        if type(current) is not dict:
            raise PiProfileUnavailable('请先核对原生 Pi 当前模型，再选择显式思考强度。')
        model_id = native_model_id(current.get('provider'), current.get('id'))
    if not is_pi_native_model_id(model_id):
        raise PiProfileUnavailable('这份配置不是 Pi 原生目录引用；不能把文字连接模型名当作原生模型。')
    rows = [row for row in _projected_rows(catalogue) if row['id'] == model_id]
    if len(rows) != 1:
        raise PiProfileUnavailable('当前 Pi 原生目录没有这对提供商与模型，请刷新或编辑配置。')
    row = rows[0]
    options = {key: row[key] for key in ('provider', 'model')}
    if effort is not None:
        if effort not in row['supported_efforts']:
            raise PiProfileUnavailable('Pi 原生 SDK 尚未声明当前模型支持这个思考强度；未降级执行。')
        options['thinking'] = 'off' if effort == 'none' else effort
    return validate_startup_options(options)


def project_pi_profiles(profiles, catalogue, *, current=None):
    """Already decoded saved profiles -> availability for this native route only."""
    if type(profiles) is not list:
        raise PiProfileUnavailable('Pi 原生配置列表格式无效。')
    result = []
    for profile in profiles:
        if type(profile) is not dict:
            raise PiProfileUnavailable('Pi 原生配置列表格式无效。')
        if profile.get('agent_instance_id') != 'local/pi' or profile.get('device_id') != 'local':
            continue
        try:
            row = {key: profile[key] for key in _PROFILE_FIELDS}
        except KeyError:
            raise PiProfileUnavailable('Pi 原生配置字段不完整。') from None
        try:
            resolve_pi_profile(profile, catalogue, current=current)
        except PiProfileUnavailable as error:
            row.update(available=False, unavailable_reason=str(error))
        else:
            row.update(available=True, unavailable_reason=None)
        result.append(row)
    return result


def project_pi_capabilities(snapshot, *, enabled):
    """Historical editing candidates, never account access or execution proof."""
    if type(enabled) is not bool:
        raise PiProfileUnavailable('Pi 原生入口状态无效。')
    result = {'enabled': enabled, 'source': 'pi/get_available_models',
        'catalogue_available': False, 'observed_at': None, 'source_chat_id': None, 'models': []}
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
            validate_startup_options({key: row[key] for key in ('provider', 'model')})
        except PiProfileUnavailable:
            continue  # Full native observation stays intact; no unsafe choice is advertised.
        result['models'].append(row)
    return result
