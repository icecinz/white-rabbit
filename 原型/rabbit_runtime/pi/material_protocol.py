"""Pi-only finite JSON fingerprints and material budgets; no Agent or I/O.

Canonical bytes match the existing native ledger exactly for legacy requests.
The larger Pi budget is local to this protocol, never a change to other Agents.
"""
from ..native_json import canonical_metadata as json_metadata, NativeJSONError


MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RPC_FRAME_BYTES = 64 * 1024 * 1024
MAX_RPC_EVENT_BYTES = 128 * 1024 * 1024
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_SESSION_RECORD_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_VIEW_MESSAGES = 200
PROMPT_CONTEXT_RESERVE_BYTES = 4 * 1024 * 1024
MAX_RPC_REQUEST_ID_CHARS = 256
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10000
MAX_RECORD_JSON_DEPTH = 256
MAX_RECORD_JSON_NODES = 2000000


class PiMaterialProtocolError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_json', 'capacity') else 'invalid_json'
        super().__init__('Pi 原生内容超过本次容量。' if self.code == 'capacity' else 'Pi 原生内容不是受支持的严格 JSON。')


def _metadata(value, *, max_bytes, request):
    try:
        return json_metadata(value,
            max_bytes=MAX_RPC_FRAME_BYTES if max_bytes is None else max_bytes,
            max_depth=MAX_JSON_DEPTH if request else MAX_RECORD_JSON_DEPTH,
            max_nodes=MAX_JSON_NODES if request else MAX_RECORD_JSON_NODES,
            safe_integers=request)
    except NativeJSONError as error:
        raise PiMaterialProtocolError(error.code) from None


def canonical_sha256(value, *, max_bytes=None):
    return canonical_metadata(value, max_bytes=max_bytes)['sha256']


def canonical_metadata(value, *, max_bytes=None):
    return _metadata(value, max_bytes=max_bytes, request=False)


def request_sha256(request):
    if type(request) is not dict:
        raise PiMaterialProtocolError('invalid_json')
    return _metadata(request, max_bytes=MAX_REQUEST_BYTES, request=True)['sha256']


def admit_prompt_context(messages, request, reserve=PROMPT_CONTEXT_RESERVE_BYTES):
    """Admit known complete context before native submission, never compact it.

    Pi's get_messages returns one unpaginated frame. Include its envelope, a
    maximally long allowed request ID, and the next original user message with
    a conservative 16-digit timestamp. Keep explicit room for future native
    assistant/tools/extension changes; those unknown outputs are not bounded by
    this preflight and may still exceed the transport limit.
    """
    if (type(messages) is not list or any(type(message) is not dict for message in messages)
            or type(request) is not dict or not {'type', 'message'} <= request.keys()
            or request.keys() - {'type', 'message', 'images'} or request['type'] != 'prompt'
            or type(request['message']) is not str
            or 'images' in request and (type(request['images']) is not list
                or any(type(image) is not dict or image.get('type') != 'image' for image in request['images']))
            or type(reserve) is not int or reserve < 0):
        raise PiMaterialProtocolError('invalid_json')
    if reserve >= MAX_RPC_FRAME_BYTES:
        raise PiMaterialProtocolError('capacity')
    user = {'role': 'user', 'content': [{'type': 'text', 'text': request['message']},
                                      *request.get('images', [])], 'timestamp': 2**53 - 1}
    envelope = {'id': 'x' * MAX_RPC_REQUEST_ID_CHARS, 'type': 'response',
                'command': 'get_messages', 'success': True,
                'data': {'messages': [*messages, user]}}
    metadata = canonical_metadata(envelope, max_bytes=MAX_RPC_FRAME_BYTES - reserve)

    def floats(value):
        if type(value) is float:
            return 1
        if type(value) is dict:
            return sum(floats(item) for item in value.values())
        if type(value) is list:
            return sum(floats(item) for item in value)
        return 0

    # The actual native encoder is JavaScript. Its finite IEEE-754 number
    # representation may be longer than Python's (e.g. decimal vs exponent).
    # 32 extra bytes per float conservatively cover the complete JS numeral;
    # integer encodings and string/key order cannot make this bound smaller.
    numeric_margin = 32 * floats(envelope)
    known = metadata['size_bytes'] + numeric_margin
    if known + reserve > MAX_RPC_FRAME_BYTES:
        raise PiMaterialProtocolError('capacity')
    return {'known_response_bytes': known, 'reserve_bytes': reserve,
            'limit_bytes': MAX_RPC_FRAME_BYTES,
            'canonical_response_bytes': metadata['size_bytes'], 'numeric_margin_bytes': numeric_margin}
