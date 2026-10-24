"""Finite answers to observed original DSH requests; no policy or execution.

The original API remains the final schema/business authority. This projection
permits the small set of one-shot controls offered by our page and binds every
answer to the exact request, original RPC identity, session and question order.
"""
from copy import deepcopy
import json

MAX_ANSWER_BYTES = 128 * 1024


def _text(value, limit=16000, *, empty=False):
    return (type(value) is str and (empty or bool(value)) and len(value) <= limit
            and not any(ord(c) < 32 and c not in '\n\r\t' or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _id(value):
    return _text(value, 256) and not any(ord(c) < 32 for c in value)


def answerable(request, session_id):
    if (type(request) is not dict or not _id(request.get('rpcId'))
            or type(request.get('payload')) is not dict):
        return False
    payload = request['payload']
    if payload.get('sessionId') != session_id:
        return False
    if payload.get('type') == 'approval/requested':
        return _id(payload.get('approvalId')) and _text(payload.get('toolName'), 512)
    if payload.get('type') != 'question/requested':
        return False
    questions = payload.get('questions')
    return (type(questions) is list and 1 <= len(questions) <= 32 and all(
        type(question) is dict and _id(question.get('id'))
        and _text(question.get('question'))
        and all(key not in question or _text(question[key], empty=True) for key in ('header', 'detail'))
        and ('multiSelect' not in question or type(question['multiSelect']) is bool)
        and ('options' not in question or type(question['options']) is list
             and len(question['options']) <= 128 and all(
                 type(option) is dict and _text(option.get('label'))
                 and ('description' not in option or _text(option['description'], empty=True))
                 for option in question['options'])) for question in questions))


def response_for(request, response, session_id):
    """Return the unchanged native response, or refuse before forwarding."""
    if not answerable(request, session_id):
        raise ValueError('Unsupported original DSH interaction.')
    try:
        if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_ANSWER_BYTES:
            raise ValueError
    except (TypeError, UnicodeError, OverflowError):
        raise ValueError('Invalid original DSH answer.') from None
    if (type(response) is not dict or set(response) != {'type', 'rpcId', 'result'}
            or response['type'] != 'client-response' or response['rpcId'] != request['rpcId']
            or type(response['result']) is not dict):
        raise ValueError('The original DSH RPC identity changed.')
    payload, result = request['payload'], response['result']
    if payload['type'] == 'question/requested' and result.get('ok') is False and result == {'ok': False, 'error': {
            'code': 'cancelled', 'message': 'the user closed this question request', 'details': {}}}:
        return deepcopy(response)
    if set(result) != {'ok', 'value'} or result['ok'] is not True or type(result['value']) is not dict:
        raise ValueError('Unsupported original DSH result.')
    value = result['value']
    if value.get('sessionId') != session_id:
        raise ValueError('The original DSH session changed.')
    if payload['type'] == 'approval/requested':
        if (set(value) != {'sessionId', 'approvalId', 'outcome'}
                or value['approvalId'] != payload['approvalId']
                or value['outcome'] not in ('allowed-once', 'rejected')):
            raise ValueError('Only a one-shot native approval is supported.')
    else:
        if (set(value) != {'sessionId', 'answer'} or type(value['answer']) is not dict
                or set(value['answer']) != {'answers'} or type(value['answer']['answers']) is not list
                or len(value['answer']['answers']) != len(payload['questions'])):
            raise ValueError('The full original question batch is required.')
        for question, answer in zip(payload['questions'], value['answer']['answers']):
            if (type(answer) is not dict or not {'id', 'selected'} <= answer.keys()
                    or answer.keys() - {'id', 'selected', 'custom'} or answer['id'] != question['id']
                    or type(answer['selected']) is not list
                    or any(type(item) is not str for item in answer['selected'])
                    or len(answer['selected']) != len(set(answer['selected']))):
                raise ValueError('Question order and original choices must be preserved.')
            selected = answer['selected']
            labels = {option['label'] for option in question.get('options', [])}
            custom = answer.get('custom')
            if (any(item not in labels for item in selected)
                    or 'custom' in answer and (not _text(custom) or not custom.strip())
                    or question.get('multiSelect') is not True and (len(selected) > 1 or custom is not None and selected)):
                raise ValueError('The answer does not match this original question.')
    return deepcopy(response)
