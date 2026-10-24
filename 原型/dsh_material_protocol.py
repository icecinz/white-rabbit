"""Finite DSH text requests and complete-event history projections.

These are presentation/input bounds, not a substitute for the original DSH
execution loop or log. No function reads a source, starts a process or retries.
"""
from copy import deepcopy
import json

from rabbit_runtime.native_json import NativeJSONError, canonical_metadata as json_metadata


MAX_USER_TEXT_BYTES = 128 * 1024
MAX_INPUT_BYTES = 900 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
MAX_HISTORY_EVENTS = 2000
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_RPC_FRAME_BYTES = 2 * 1024 * 1024
MAX_RPC_EVENT_BYTES = 16 * 1024 * 1024
_MAX_INT = 2**53 - 1


class DshMaterialProtocolError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('DSH完整输入超过容量。' if code == 'capacity' else 'DSH原生文本协议无效。')


def canonical_metadata(value, *, max_bytes=MAX_INPUT_BYTES):
    try:
        return json_metadata(value, max_bytes=max_bytes, max_depth=64,
                             max_nodes=200000, safe_integers=True)
    except NativeJSONError as error:
        raise DshMaterialProtocolError(error.code) from None


def request(message, *, original_message=None):
    """Hash the actual single native text input, including JSON escaping."""
    try:
        if type(message) is not str or not message.strip():
            raise DshMaterialProtocolError('invalid_request')
        if original_message is not None:
            if (type(original_message) is not str or not original_message.strip()
                    or len(original_message.encode('utf-8')) > MAX_USER_TEXT_BYTES
                    or not (message == original_message or message.startswith(original_message + '\n\n'))):
                raise DshMaterialProtocolError('invalid_request')
        value = {'type': 'prompt', 'message': message}
        canonical_metadata(value)
        return value
    except UnicodeError:
        raise DshMaterialProtocolError('invalid_request') from None


def _bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DshMaterialProtocolError('invalid_history') from None


def project_history(value, *, wrap=None, max_bytes=MAX_HISTORY_BYTES):
    """Keep an intact bounded tail; original seq/range and omissions remain.

    ``wrap`` is a trusted host function measuring its actual response envelope.
    A single oversized non-stream event is an error, never a truncated message.
    The returned objects retain original fields and string bytes unchanged.
    """
    if (type(value) is not dict or type(value.get('events')) is not list
            or type(value.get('hasMore')) is not bool or type(max_bytes) is not int
            or max_bytes <= 0 or wrap is not None and not callable(wrap)):
        raise DshMaterialProtocolError('invalid_history')
    source = value['events']
    previous = -1
    for row in source:
        event = row.get('event') if type(row) is dict else None
        seq = event.get('seq') if type(event) is dict else None
        if type(seq) is not int or not previous < seq <= _MAX_INT:
            raise DshMaterialProtocolError('invalid_history')
        previous = seq
    inherited = value.get('omitted_stream_chunks', 0)
    omitted = value.get('omitted_history_events', 0)
    if any(type(n) is not int or not 0 <= n <= _MAX_INT for n in (inherited, omitted)):
        raise DshMaterialProtocolError('invalid_history')
    events = [row for row in source if row['event'].get('type') != 'assistant/chunk']
    chunks = inherited + len(source) - len(events)
    if chunks > _MAX_INT:
        raise DshMaterialProtocolError('invalid_history')
    window = value.get('source_window', {
        'first_seq': source[0]['event']['seq'] if source else None,
        'last_seq': source[-1]['event']['seq'] if source else None,
        'event_count': len(source)})
    if (type(window) is not dict or set(window) != {'first_seq', 'last_seq', 'event_count'}
            or type(window['event_count']) is not int or not len(source) <= window['event_count'] <= _MAX_INT
            or (window['event_count'] == 0 and (window['first_seq'] is not None or window['last_seq'] is not None))
            or (window['event_count'] > 0 and (type(window['first_seq']) is not int
                or type(window['last_seq']) is not int
                or not 0 <= window['first_seq'] <= window['last_seq'] <= _MAX_INT))
            or source and not window['first_seq'] <= source[0]['event']['seq'] <= source[-1]['event']['seq'] <= window['last_seq']):
        raise DshMaterialProtocolError('invalid_history')

    def make(rows, dropped):
        out = dict(value, events=rows, hasMore=value['hasMore'] or dropped > 0 or omitted > 0,
                   omitted_stream_chunks=chunks, source_window=deepcopy(window))
        if omitted or dropped:
            out['omitted_history_events'] = omitted + dropped
        return out

    def size(out):
        return len(_bytes(wrap(out) if wrap is not None else out))

    if size(make([], len(events))) > max_bytes:
        raise DshMaterialProtocolError('history_capacity')
    # Explicitly reject an event that cannot be displayed intact, even when it
    # lies before the tail. A normal large file remains one complete event.
    for row in events:
        if size(make([row], max(0, len(events) - 1))) > max_bytes:
            raise DshMaterialProtocolError('history_event_capacity')
    low, high = 0, min(len(events), MAX_HISTORY_EVENTS)
    while low < high:
        count = (low + high + 1) // 2
        if size(make(events[-count:], len(events) - count)) <= max_bytes:
            low = count
        else:
            high = count - 1
    return make(events[-low:] if low else [], len(events) - low)
