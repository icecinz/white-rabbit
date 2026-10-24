"""Pure windows over Hermes' complete DISPLAY message projection.

These messages come from ``session.activate``. They are not raw database rows:
Hermes already shapes display text, tools, skills and compaction markers. This
module never changes that source snapshot or supplies an Agent/RPC/file reader.
"""
import base64
import binascii
from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


MAX_NATIVE_VIEW_BYTES = 7 * 1024 * 1024
MAX_NATIVE_VIEW_MESSAGES = 20000
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_VIEW_MESSAGES = 200
PROMPT_CONTEXT_RESERVE_BYTES = 1024 * 1024
NATIVE_METADATA_MARGIN_BYTES = 64 * 1024
MAX_JSON_DEPTH = 256
MAX_JSON_NODES = 2000000
MAX_DISPLAY_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_REFERENCE_QUOTE = re.compile(r'''[\s()\[\]{}<>"'`]''')


class HermesMessageViewError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_messages', 'invalid_json', 'invalid_cursor',
            'invalid_reference', 'message_changed', 'native_capacity', 'view_capacity',
            'invalid_image', 'image_capacity') else 'invalid_messages'
        descriptions = {
            'message_changed': 'Hermes 原生显示消息已变化，请刷新后重新读取。',
            'native_capacity': 'Hermes 完整显示记录超出本次容量，原记录未修改。',
            'view_capacity': 'Hermes 消息超出本次页面容量，原记录未修改。',
            'invalid_image': 'Hermes 完整显示消息没有可核验的这张原图。',
            'image_capacity': 'Hermes 原图超出本次读取容量。',
        }
        super().__init__(descriptions.get(self.code, 'Hermes 原生显示消息或引用无法完整核验。'))


def _validate(value):
    active, count = set(), 0

    def check(item, depth=0):
        nonlocal count
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise HermesMessageViewError('invalid_json')
        kind = type(item)
        if item is None or kind in (bool, int):
            return
        if kind is float:
            if not math.isfinite(item):
                raise HermesMessageViewError('invalid_json')
            return
        if kind is str:
            item.encode('utf-8', 'strict')
            return
        if kind not in (list, dict) or id(item) in active:
            raise HermesMessageViewError('invalid_json')
        active.add(id(item))
        try:
            if kind is dict:
                for key, nested in item.items():
                    if type(key) is not str:
                        raise HermesMessageViewError('invalid_json')
                    check(key, depth + 1)
                    check(nested, depth + 1)
            else:
                for nested in item:
                    check(nested, depth + 1)
        finally:
            active.remove(id(item))

    check(value)


def _canonical_parts(value):
    return json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(',', ':')).iterencode(value)


def canonical_metadata(value, *, max_bytes=None):
    """Hash standard Python canonical JSON spelling, including Unicode/floats.

    Callers that deliver exact JSON must hash the returned string's UTF-8 bytes;
    decoding and re-encoding in JavaScript is not an equivalent fingerprint.
    The whole JSON document is not joined just to measure or hash it.
    """
    limit = MAX_NATIVE_VIEW_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit <= 0:
        raise HermesMessageViewError('invalid_json')
    try:
        _validate(value)
        size, digest = 0, hashlib.sha256()
        for part in _canonical_parts(value):
            encoded = part.encode('utf-8', 'strict')
            size += len(encoded)
            if size > limit:
                raise HermesMessageViewError('native_capacity')
            digest.update(encoded)
        return {'sha256': digest.hexdigest(), 'size_bytes': size}
    except HermesMessageViewError:
        raise
    except (RuntimeError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise HermesMessageViewError('invalid_json') from None


def validate_messages(messages):
    """Validate the complete finite native DISPLAY snapshot, without copying it."""
    if type(messages) is not list or any(type(message) is not dict for message in messages):
        raise HermesMessageViewError('invalid_messages')
    if len(messages) > MAX_NATIVE_VIEW_MESSAGES:
        raise HermesMessageViewError('native_capacity')
    return canonical_metadata(messages)


def _display_image(url):
    """Decode one exact data URL. No path, network, image library or file I/O."""
    if type(url) is not str:
        raise HermesMessageViewError('invalid_image')
    header, comma, encoded = url.partition(',')
    if not comma or header not in ('data:image/png;base64', 'data:image/jpeg;base64'):
        raise HermesMessageViewError('invalid_image')
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise HermesMessageViewError('image_capacity')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise HermesMessageViewError('invalid_image') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise HermesMessageViewError('image_capacity')
    mime = header[5:-7]
    signature = b'\x89PNG\r\n\x1a\n' if mime == 'image/png' else b'\xff\xd8\xff'
    if not raw.startswith(signature):
        raise HermesMessageViewError('invalid_image')
    return {'data_base64': encoded, 'mime': mime, 'size_bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest()}


def split_display_images(text):
    """Split only a complete, canonical, contiguous tail of 1–4 data URL lines.

    Hermes flattens native user content into text plus a newline per image URL.
    A malformed/over-limit tail is left WHOLE, never partially consumed. Bare
    paths, @image directives, network URLs and embedded examples stay text.
    This is a formatting/byte-read contract, not proof of model vision or of a
    particular attachment sender. It grants no filesystem access.
    """
    if type(text) is not str:
        raise HermesMessageViewError('invalid_json')
    original = {'text': text, 'images': []}
    end, suffix = len(text), []
    while True:
        separator = text.rfind('\n', 0, end)
        line = text[separator + 1:end]
        if not line.startswith('data:'):
            break
        suffix.append(line)
        if len(suffix) > MAX_DISPLAY_IMAGES:
            return original
        end = separator
        if separator < 0:
            break
    if not suffix:
        return original
    try:
        images = [dict(image_index=index, **_display_image(url))
            for index, url in enumerate(reversed(suffix))]
    except HermesMessageViewError:
        return original
    return {'text': text[:max(0, end)], 'images': images}


def _project_images(message):
    if (message.get('role') != 'user' or type(message.get('text')) is not str
            or 'native_images' in message):
        return message, False
    split = split_display_images(message['text'])
    if not split['images']:
        return message, False
    row = {key: value for key, value in message.items() if key != 'text'}
    row['text'] = split['text']
    row['native_images'] = [{key: value for key, value in image.items()
        if key != 'data_base64'} | {'display_omitted': True} for image in split['images']]
    return row, True


def _placeholder(message, metadata):
    role = message.get('role')
    return {'type': 'rabbit_message_placeholder',
        'role': role if type(role) is str and len(role) <= 128 else 'unknown',
        'original_sha256': metadata['sha256'], 'original_size_bytes': metadata['size_bytes'],
        'reason': 'display_capacity'}


def project_messages(messages, before=None, *, projector=None):
    """Return a contiguous bounded page; ``before`` is an exclusive index.

    Oversized single messages become explicit placeholders. Smaller messages are
    kept whole. References always fingerprint their complete source message.
    """
    validate_messages(messages)
    if projector is not None and not callable(projector):
        raise HermesMessageViewError('invalid_json')
    total = len(messages)
    if before is not None and (type(before) is not int or not 0 <= before <= total):
        raise HermesMessageViewError('invalid_cursor')
    end = total if before is None else before
    rows, references, row_bytes, reference_bytes = [], [], 0, 0
    start, projected_any = end, False

    def info(first, changed):
        return {'start': first, 'end': end, 'total': total, 'has_more_before': first > 0,
            'has_more_after': end < total, 'projected': changed}

    def page_size(first, changed, count, content_bytes, ref_bytes):
        empty = {'native_messages': [], 'native_message_refs': [],
            'native_messages_info': info(first, changed)}
        return (canonical_metadata(empty)['size_bytes'] + content_bytes + ref_bytes
            + 2 * max(0, count - 1))

    for index in range(end - 1, max(-1, end - MAX_VIEW_MESSAGES - 1), -1):
        message = messages[index]
        metadata = canonical_metadata(message)
        row, changed = _project_images(message)
        if projector is not None:
            extra = projector(message, row)
            if extra is not None:
                if type(extra) is not dict:
                    raise HermesMessageViewError('invalid_json')
                row, changed = extra, True
        length = canonical_metadata(row)['size_bytes'] if changed else metadata['size_bytes']
        reference = {'index': index, 'sha256': metadata['sha256'], 'projected': changed}
        ref_length = canonical_metadata(reference)['size_bytes']
        if page_size(index, changed, 1, length, ref_length) > MAX_VIEW_BYTES:
            row, changed = _placeholder(message, metadata), True
            length = canonical_metadata(row)['size_bytes']
            reference['projected'] = True
            ref_length = canonical_metadata(reference)['size_bytes']
        if page_size(index, projected_any or changed, len(rows) + 1,
                row_bytes + length, reference_bytes + ref_length) > MAX_VIEW_BYTES:
            if rows:
                break
            raise HermesMessageViewError('view_capacity')
        rows.append(deepcopy(row))
        references.append(reference)
        row_bytes += length
        reference_bytes += ref_length
        start, projected_any = index, projected_any or changed
    result = {'native_messages': list(reversed(rows)), 'native_message_refs': list(reversed(references)),
        'native_messages_info': info(start, projected_any)}
    if page_size(start, projected_any, len(rows), row_bytes, reference_bytes) > MAX_VIEW_BYTES:
        raise HermesMessageViewError('view_capacity')
    return result


def original_message(messages, index, sha256):
    """Return exact canonical JSON STRING for one complete DISPLAY message."""
    validate_messages(messages)
    if (type(index) is not int or not 0 <= index < len(messages)
            or type(sha256) is not str or _SHA.fullmatch(sha256) is None):
        raise HermesMessageViewError('invalid_reference')
    message = messages[index]
    if canonical_metadata(message)['sha256'] != sha256:
        raise HermesMessageViewError('message_changed')
    # The complete snapshot and this message were bounded above. Join only for
    # an explicit exact-read operation, never every polling/display request.
    return ''.join(_canonical_parts(message))


def original_image(messages, index, image_index, sha256):
    """Read an exact original image from the cached official DISPLAY string.

    The SHA addresses the COMPLETE original display message, not the projected
    text or image. This function cannot resolve a path or invoke an RPC.
    """
    validate_messages(messages)
    if (type(index) is not int or not 0 <= index < len(messages)
            or type(image_index) is not int or not 0 <= image_index < MAX_DISPLAY_IMAGES
            or type(sha256) is not str or _SHA.fullmatch(sha256) is None):
        raise HermesMessageViewError('invalid_reference')
    message = messages[index]
    if canonical_metadata(message)['sha256'] != sha256:
        raise HermesMessageViewError('message_changed')
    if message.get('role') != 'user' or type(message.get('text')) is not str:
        raise HermesMessageViewError('invalid_image')
    images = split_display_images(message['text'])['images']
    if image_index >= len(images):
        raise HermesMessageViewError('invalid_image')
    return {key: value for key, value in images[image_index].items() if key != 'image_index'}


def _float_count(value):
    if type(value) is float:
        return 1
    if type(value) is dict:
        return sum(_float_count(item) for item in value.values())
    if type(value) is list:
        return sum(_float_count(item) for item in value)
    return 0


def admit_prompt_context(messages, message, reserve=PROMPT_CONTEXT_RESERVE_BYTES,
        *, native_metadata_bytes=0):
    """Reject a known over-capacity whole input before native submission.

    The native DISPLAY response is unpaginated. Account for its complete known
    history and a prospective user message, plus the known current response
    bytes outside its messages and 64KiB of additional metadata margin. The
    caller supplies that measured byte count, never the private metadata.
    A separate 1MiB default output reserve cannot pay for known metadata.
    Unknown future assistant/tool output can still grow beyond that reserve;
    this is not a context-window or performance guarantee.
    """
    validate_messages(messages)
    if (type(message) is not str or type(reserve) is not int or reserve < 0
            or type(native_metadata_bytes) is not int or native_metadata_bytes < 0):
        raise HermesMessageViewError('invalid_json')
    if (reserve >= MAX_NATIVE_VIEW_BYTES or native_metadata_bytes >= MAX_NATIVE_VIEW_BYTES
            or len(messages) >= MAX_NATIVE_VIEW_MESSAGES):
        raise HermesMessageViewError('native_capacity')
    user = {'role': 'user', 'text': message, 'timestamp': 2**53 - 1,
        'row_id': 2**53 - 1}
    envelope = {'id': 2**53 - 1, 'result': {'ok': True, 'event_seq': 2**53 - 1,
        'value': {'session_id': 'x' * 128, 'session_key': 'x' * 128,
            'messages': [*messages, user], 'running': False}}}
    metadata = canonical_metadata(envelope)
    # The worker also JSON.stringifies the native result; allow for a longer
    # finite IEEE-754 numeral than Python's canonical spelling.
    margin = NATIVE_METADATA_MARGIN_BYTES + native_metadata_bytes + 32 * _float_count(envelope)
    known = metadata['size_bytes'] + margin
    if known + reserve > MAX_NATIVE_VIEW_BYTES:
        raise HermesMessageViewError('native_capacity')
    return {'known_response_bytes': known, 'reserve_bytes': reserve,
        'limit_bytes': MAX_NATIVE_VIEW_BYTES,
        'canonical_response_bytes': metadata['size_bytes'], 'metadata_margin_bytes': margin}


def _admission_images(images):
    if type(images) is not list or not 1 <= len(images) <= MAX_DISPLAY_IMAGES:
        raise HermesMessageViewError('invalid_image')
    result = []
    for image in images:
        if (type(image) is not dict or set(image) != {'path', 'sha256', 'size_bytes', 'mime'}
                or type(image['path']) is not str or not image['path'] or len(image['path']) > 4096
                or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                       for char in image['path'])
                or type(image['sha256']) is not str or _SHA.fullmatch(image['sha256']) is None
                or type(image['size_bytes']) is not int or image['size_bytes'] <= 0
                or type(image['mime']) is not str or image['mime'] not in ('image/png', 'image/jpeg')):
            raise HermesMessageViewError('invalid_image')
        path = PurePosixPath(image['path'])
        if not path.is_absolute() or '..' in path.parts or str(path) != image['path']:
            raise HermesMessageViewError('invalid_image')
        if image['size_bytes'] > MAX_IMAGE_BYTES:
            raise HermesMessageViewError('image_capacity')
        result.append(image)
    return result


def reference_value(value):
    # Exact string-only rule from the pinned Hermes
    # agent/context_references.py:format_reference_value. Never inspect a path.
    if not _REFERENCE_QUOTE.search(value):
        return value
    for quote in ('`', '"', "'"):
        if quote not in value:
            return quote + value + quote
    return value


def admit_image_context(messages, derived_text, images,
        reserve=PROMPT_CONTEXT_RESERVE_BYTES, *, native_metadata_bytes=0):
    """Admit complete known history + both original Hermes attachment routes.

    Only verified image DESCRIPTORS are needed. Base64 consists of ASCII JSON
    safe characters, so its exact length can be added to a small, real JSON
    envelope without allocating or inventing image bytes. The caller still
    verifies the original bytes and the official attach/submit acknowledgments.

    Native persisted display: caption + @image reference lines + data URL
    lines. A native in-memory fallback can instead show the model-side path
    hints; a text-routed fallback includes its two-path vision-tool hints. All
    four known forms are budgeted and the largest wins. Each includes the
    new inflight.user copy independently of the future output reserve.

    Unknown @reference expansion, future native notes or arbitrary tool/model
    output can exceed the margin. This is neither a context-window guarantee
    nor a reason to edit, compact, resize or truncate original history/images.
    No filesystem, Agent, native import, RPC or model call occurs here.
    """
    validate_messages(messages)
    if (type(derived_text) is not str or not derived_text.strip()
            or type(reserve) is not int or reserve < 0
            or type(native_metadata_bytes) is not int or native_metadata_bytes < 0):
        raise HermesMessageViewError('invalid_json')
    if reserve >= MAX_NATIVE_VIEW_BYTES:
        raise HermesMessageViewError('native_capacity')
    images = _admission_images(images)
    # Validate Unicode before constructing route strings or measuring paths.
    canonical_metadata(derived_text)
    paths = [image['path'] for image in images]
    persisted = derived_text + '\n' + '\n'.join('@image:' + reference_value(path) for path in paths)
    headers = ''.join('\ndata:' + image['mime'] + ';base64,' for image in images)
    base64_bytes = sum(4 * ((image['size_bytes'] + 2) // 3) for image in images)
    native_runtime = derived_text.strip() + '\n\n' + '\n'.join(
        '[Image attached at: ' + path + ']' for path in paths)
    text_runtime = '\n\n'.join(
        '[The user attached an image: ' + PurePosixPath(path).name + ']\n'
        '[Examine it with the vision_analyze tool using image_url: ' + path + ']'
        for path in paths) + '\n\n' + derived_text
    # _start_inflight_turn sees the submitted STRING before image routing.
    # Count the full future inflight JSON, including its known wrapper. Keeping
    # current non-message metadata as well is deliberately conservative.
    inflight_bytes = canonical_metadata({'inflight': {'assistant': '',
        'streaming': True, 'user': derived_text.strip()}})['size_bytes']
    routes = (
        ('native_persisted', persisted + headers, base64_bytes),
        ('native_runtime', native_runtime + headers, base64_bytes),
        ('text_persisted', persisted, 0),
        ('text_runtime', text_runtime, 0),
    )
    budgets = {}
    for name, display, binary_bytes in routes:
        # The display holds empty base64 strings for measurement. Add their
        # exact safe-ASCII lengths as KNOWN input, never paid from reserve.
        budget = admit_prompt_context(messages, display, reserve=0,
            native_metadata_bytes=native_metadata_bytes)
        known_input_extra = inflight_bytes + binary_bytes
        known = budget['known_response_bytes'] + known_input_extra
        if known + reserve > MAX_NATIVE_VIEW_BYTES:
            raise HermesMessageViewError('native_capacity')
        budgets[name] = dict(budget, known_response_bytes=known, reserve_bytes=reserve,
            known_input_extra_bytes=known_input_extra, image_base64_bytes=binary_bytes,
            known_inflight_bytes=inflight_bytes,
            display_text_json_bytes=canonical_metadata(display)['size_bytes'] + binary_bytes)
    limiting = max(budgets, key=lambda name: budgets[name]['known_response_bytes'])
    return {**budgets[limiting], 'limiting_route': limiting, 'route_budgets': budgets,
        'native_metadata_bytes': native_metadata_bytes, 'image_count': len(images)}
