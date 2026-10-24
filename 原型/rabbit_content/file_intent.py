"""Separate per-turn file intent; never part of an AI/memory profile."""
import re
import uuid


def _id(value):
    if not isinstance(value, str):
        raise ValueError('文件成果标识无效。')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError('文件成果标识无效。') from None
    if str(parsed) != value:
        raise ValueError('文件成果标识无效。')
    return value


def validate_intent(value=None):
    if value is None:
        return {'mode': 'chat', 'artifact_ids': []}
    if (not isinstance(value, dict) or set(value) != {'mode', 'artifact_ids'}
            or value['mode'] not in ('chat', 'files')
            or not isinstance(value['artifact_ids'], list) or len(value['artifact_ids']) > 4):
        raise ValueError('本轮文件任务选择无效。')
    ids = [_id(item) for item in value['artifact_ids']]
    if len(set(ids)) != len(ids):
        raise ValueError('请为本轮选择最多四份不同的文本文件。')
    return {'mode': value['mode'], 'artifact_ids': ids}


def validate_inputs(value, intent):
    intent = validate_intent(intent)
    value = [] if value is None else value
    if not isinstance(value, list) or len(value) != len(intent['artifact_ids']):
        raise ValueError('文件输入与本轮选择不同。')
    result = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'id', 'sha256'}
                or not isinstance(item['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
            raise ValueError('文件输入版本无效。')
        result.append({'id': _id(item['id']), 'sha256': item['sha256']})
    if [item['id'] for item in result] != intent['artifact_ids']:
        raise ValueError('文件输入与本轮选择不同。')
    return result
