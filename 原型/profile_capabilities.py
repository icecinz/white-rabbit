"""Pure, bounded effort declarations for a selected model's exact cache row.

The caller must select the row by the actual requested model ID first. This
module never reads a cache, path, credential or model service. The vocabulary
below validates known wire values; it never supplies missing model abilities.

``default_effort`` describes the cache's declared default, not the adapter's
current execution policy. ``None`` as a profile selection preserves the intent
"Agent default"; it is never silently changed to medium/ultra/max here.
"""
from agent_registry import has as registered_agent, LABELS


KNOWN_EFFORTS = frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
MAX_REASONING_LEVEL_ROWS = 32
_LEVEL_FIELDS = ("supported_reasoning_levels", "supportedReasoningEfforts")
_DEFAULT_FIELDS = ("default_reasoning_level", "defaultReasoningEffort")
_ENTRY_FIELDS = ("effort", "reasoningEffort")
_NO_EXPLICIT = "Codex 精确模型缓存未声明可选强度；仅支持 Agent 默认。"
_INVALID_LEVELS = "Codex 精确模型缓存的强度列表格式无效；仅支持 Agent 默认。"
_CONFLICTING_LEVELS = "Codex 精确模型缓存的强度声明互相冲突；仅支持 Agent 默认。"
_HERMES_DEFAULT = "Hermes 当前适配器仅支持 Agent 默认，未接入独立强度参数。"


def _known_effort(value):
    return isinstance(value, str) and len(value) <= 16 and value in KNOWN_EFFORTS


def _parse_levels(value):
    if not isinstance(value, list) or len(value) > MAX_REASONING_LEVEL_ROWS:
        return None
    result = []
    for item in value:
        # The known cache/API shapes contain records, not scalar string lists.
        if not isinstance(item, dict):
            return None
        values = [item[key] for key in _ENTRY_FIELDS if key in item]
        if not values or any(not isinstance(effort, str) or len(effort) > 16 for effort in values):
            return None
        if len(values) == 2 and values[0] != values[1]:
            return None
        effort = values[0]
        # Future/unknown wire values are not inferred, normalized or exposed.
        # A known sibling still has explicit evidence in its own record.
        if _known_effort(effort) and effort not in result:
            result.append(effort)
    return result


def _declared_default(model_row, supported):
    values = [model_row[key] for key in _DEFAULT_FIELDS if key in model_row]
    if not values or any(not _known_effort(value) for value in values):
        return None
    if len(values) == 2 and values[0] != values[1]:
        return None
    default = values[0]
    # A valid default alone is informative, but never a selectable-effort list.
    return default if not supported or default in supported else None


def model_effort_capability(backend, model_row):
    """Return a fixed public capability using only explicit selected-row data.

    Duplicate levels collapse in declaration order. Malformed/overlong arrays
    and contradictory snake/camel aliases fail closed. Unknown level strings
    are skipped, without inventing every globally known effort for this model.
    Hermes ignores row declarations until its adapter implements that control.
    """
    if not registered_agent(backend):
        raise ValueError("请选择已登记的助手。")
    result = {"supported_efforts": [], "default_effort": None,
              "explicit_effort_supported": False,
              "effort_source": (_NO_EXPLICIT if backend == 'codex' else _HERMES_DEFAULT if backend == 'hermes'
                                else LABELS[backend] + ' 当前适配器仅支持 Agent 默认，未接入独立强度参数。')}
    if backend != "codex" or not isinstance(model_row, dict):
        return result
    fields = [field for field in _LEVEL_FIELDS if field in model_row]
    supported = []
    if fields:
        parsed = [_parse_levels(model_row[field]) for field in fields]
        if any(levels is None for levels in parsed):
            result["effort_source"] = _INVALID_LEVELS
            return result
        if len(parsed) == 2 and set(parsed[0]) != set(parsed[1]):
            result["effort_source"] = _CONFLICTING_LEVELS
            return result
        supported = parsed[0]
        if supported:
            result.update(supported_efforts=supported, explicit_effort_supported=True,
                effort_source="Codex 精确模型缓存：" + " / ".join(fields) + " 的强度声明。")
    result["default_effort"] = _declared_default(model_row, supported)
    if result["default_effort"] is not None and not fields:
        result["effort_source"] = "Codex 精确模型缓存仅声明默认强度；可跟随 Agent 默认，不支持显式强度选择。"
    return result


def validate_profile_effort(capability, selected):
    """Return the exact requested effort, or None for "Agent default".

    Model/connection availability must still be checked by the route resolver.
    A default-only declaration does not authorize an explicit override. Extra
    public model fields are harmless, allowing a catalog entry as capability.
    """
    if selected is None:
        return None
    if not _known_effort(selected):
        raise ValueError("强度选择无效；请使用 Agent 默认或该模型明确支持的强度。")
    if not isinstance(capability, dict) or capability.get("explicit_effort_supported") is not True:
        raise ValueError("该模型未确认支持显式强度；请使用 Agent 默认。")
    supported = capability.get("supported_efforts")
    if (not isinstance(supported, list) or not 0 < len(supported) <= len(KNOWN_EFFORTS)
            or any(not _known_effort(value) for value in supported)
            or len(set(supported)) != len(supported) or selected not in supported):
        raise ValueError("该模型不支持这个强度，请重新选择；不会自动降级。")
    return selected


def public_effort_capability(backend, capability=None):
    """Project an adapter declaration without copying private/unknown fields.

    Only Codex currently implements explicit overrides. A malformed public
    list is unavailable rather than a partially truncated permission list.
    The selected-model adapter remains responsible for its actual evidence.
    """
    fallback = model_effort_capability(backend, {})
    if backend != 'codex' or not isinstance(capability, dict):
        return fallback
    levels = capability.get('supported_efforts')
    explicit = capability.get('explicit_effort_supported')
    if type(explicit) is not bool or not isinstance(levels, list):
        return fallback
    if explicit:
        try:
            if not levels:
                return fallback
            validate_profile_effort(capability, levels[0])
        except ValueError:
            return fallback
    elif levels:
        return fallback
    default = capability.get('default_effort')
    result = {'supported_efforts': list(levels),
              'default_effort': default if _known_effort(default) and (not levels or default in levels) else None,
              'explicit_effort_supported': explicit, 'effort_source': fallback['effort_source']}
    source = capability.get('effort_source')
    public_sources = {_NO_EXPLICIT, _INVALID_LEVELS, _CONFLICTING_LEVELS,
        "Codex 精确模型缓存仅声明默认强度；可跟随 Agent 默认，不支持显式强度选择。"}
    public_sources.update("Codex 精确模型缓存：" + fields + " 的强度声明。" for fields in
        ('supported_reasoning_levels', 'supportedReasoningEfforts', 'supported_reasoning_levels / supportedReasoningEfforts'))
    if isinstance(source, str) and 0 < len(source) <= 512 and source in public_sources:
        result['effort_source'] = source
    elif explicit:
        # Adapter diagnostics or arbitrary source prose may contain secrets;
        # only this module's fixed public evidence labels cross the catalog.
        result['effort_source'] = "Codex 接入器声明的精确模型可选强度；来源详情未确认。"
    return result
