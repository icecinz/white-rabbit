"""Saved profile -> original Codex catalogue, without the text adapter policy.

Empty fields mean inherit the native thread's current settings. This is not a
reset to initial defaults. The native catalogue is discovery, not entitlement
or per-turn model telemetry. No route, permission, provider or cwd is changed.
"""
from collections import Counter

from agent_registry import valid_model_id
from profiles import SNAPSHOT_PROFILE_FIELDS


class NativeProfileUnavailable(ValueError):
    pass


def require_image_support(catalogue, native_options):
    """Require an explicit model's observed native image capability.

    Inherited/current/default models are deliberately not resolved here. This
    check does not select a model or prove model execution; the caller sends the
    same frozen options through the original native request.
    """
    if (type(native_options) is not dict or type(native_options.get('model')) is not str
            or not native_options['model'].strip()):
        raise NativeProfileUnavailable('发送原图前，请先保存并选择一份明确指定模型的 Codex 配置。')
    if type(catalogue) is not dict or type(catalogue.get('data')) is not list:
        raise NativeProfileUnavailable('当前原生 Codex 模型目录不可核对，图片尚未发送。')
    rows = catalogue['data']
    if any(type(row) is not dict for row in rows):
        raise NativeProfileUnavailable('当前原生 Codex 模型目录格式无效，图片尚未发送。')
    matches = [row for row in rows if row.get('model') == native_options['model']]
    if len(matches) != 1:
        raise NativeProfileUnavailable('当前原生 Codex 目录无法唯一确认所选模型，图片尚未发送。')
    modalities = matches[0].get('inputModalities')
    if (type(modalities) is not list or any(type(value) is not str
            or value not in ('text', 'image', 'audio') for value in modalities)
            or 'image' not in modalities):
        raise NativeProfileUnavailable('所选模型的原生目录未明确声明图片输入能力，图片尚未发送。')


def resolve_native_profile(profile, catalogue):
    if profile['agent_instance_id'] != 'local/codex' or profile['device_id'] != 'local':
        raise NativeProfileUnavailable('这份配置不属于本机 Codex。')
    model_id, effort = profile['model_id'], profile['reasoning_effort']
    if not model_id:
        if effort is not None:
            raise NativeProfileUnavailable('请先在配置中指定模型，再选择推理强度。')
        return {}
    prefix = 'local/codex/'
    if not model_id.startswith(prefix) or not model_id[len(prefix):]:
        raise NativeProfileUnavailable('这份配置没有有效的 Codex 模型编号。')
    model = model_id[len(prefix):]
    rows = [row for row in catalogue['data'] if row['model'] == model]
    if len(rows) != 1:
        raise NativeProfileUnavailable('当前原生 Codex 目录中没有这个模型，请刷新或编辑配置。')
    options = {'model': model}
    if effort is not None:
        supported = {item['reasoningEffort'] for item in rows[0]['supportedReasoningEfforts']}
        if effort not in supported:
            raise NativeProfileUnavailable('当前原生模型目录未声明支持这个推理强度。')
        options['effort'] = effort
    return options


def project_native_profiles(profiles, catalogue):
    result = []
    for profile in profiles:
        # This list has one responsibility: saved choices for this Agent.
        if profile['agent_instance_id'] != 'local/codex' or profile['device_id'] != 'local':
            continue
        row = {key: profile[key] for key in SNAPSHOT_PROFILE_FIELDS}
        row['image_input'] = 'unknown'
        try:
            options = resolve_native_profile(profile, catalogue)
        except NativeProfileUnavailable as error:
            row.update(available=False, unavailable_reason=str(error))
        else:
            row.update(available=True, unavailable_reason=None)
            if options.get('model'):
                matches = [item for item in catalogue['data'] if item['model'] == options['model']]
                if len(matches) == 1:
                    modalities = matches[0].get('inputModalities')
                    if (type(modalities) is list and all(type(value) is str
                            and value in ('text', 'image', 'audio') for value in modalities)):
                        row['image_input'] = 'supported' if 'image' in modalities else 'unsupported'
        result.append(row)
    return result


def project_native_capabilities(snapshot, *, enabled):
    """Public discovery projection; no native health check or text fallback.

    A successful empty native directory is still an available observation.
    The timestamp and source chat identify its limited scope. A null chat ID
    means the already-ready specialist supplied it, without a normal chat.
    """
    result = {'enabled': bool(enabled), 'source': 'codex/model/list',
              'catalogue_available': False, 'observed_at': None,
              'source_chat_id': None, 'models': []}
    if not enabled or snapshot is None:
        return result
    result.update(catalogue_available=True, observed_at=snapshot['observed_at'],
                  source_chat_id=snapshot['source_chat_id'])
    rows = snapshot['catalogue']['data']
    counts = Counter(row['model'] for row in rows)
    for row in rows:
        # These are saved-profile editing candidates, not a second raw native
        # directory. Keep the full observation in RAM; omit identifiers the
        # existing saved-profile syntax cannot represent, and ambiguous rows
        # that resolve_native_profile would correctly refuse to execute.
        identity = 'local/codex/' + row['model']
        if not valid_model_id(identity, 'codex') or counts[row['model']] != 1:
            continue
        efforts = list(dict.fromkeys(item['reasoningEffort'] for item in row['supportedReasoningEfforts']))
        result['models'].append({'id': identity, 'agent_id': 'codex',
            'model': row['model'], 'name': row['displayName'], 'supported_efforts': efforts,
            'default_effort': row['defaultReasoningEffort'],
            'explicit_effort_supported': bool(efforts)})
    return result
