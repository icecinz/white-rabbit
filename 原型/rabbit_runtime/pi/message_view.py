"""Read-only Pi message windows; projections never replace native messages.

Image payloads and oversized messages have explicit placeholders. Exact
original reads are bound to a complete canonical message SHA, not just an index.
"""
import base64
import binascii
from copy import deepcopy
import hashlib
import re

from .material_protocol import (MAX_IMAGE_BYTES, MAX_VIEW_BYTES, MAX_VIEW_MESSAGES,
    PiMaterialProtocolError, canonical_metadata)


_SHA = re.compile(r'[0-9a-f]{64}\Z')


class PiMessageViewError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_messages', 'invalid_cursor', 'invalid_reference',
            'message_changed', 'invalid_image', 'image_capacity', 'view_capacity') else 'invalid_messages'
        messages = {'message_changed': 'Pi 原消息版本已变化，请刷新后重新读取。',
                    'image_capacity': 'Pi 原图超过本次读取容量。',
                    'view_capacity': 'Pi 消息超过本次显示容量，原记录未修改。'}
        super().__init__(messages.get(self.code, 'Pi 原消息或原图引用无法核验。'))


def _messages(messages):
    if type(messages) is not list or any(type(message) is not dict for message in messages):
        raise PiMessageViewError('invalid_messages')


def _fingerprint(message):
    try:
        return canonical_metadata(message)
    except PiMaterialProtocolError:
        raise PiMessageViewError('invalid_messages') from None


def _image(part):
    if (type(part) is not dict or part.get('type') != 'image'
            or part.get('mimeType') not in ('image/png', 'image/jpeg')
            or type(part.get('data')) is not str or not part['data']):
        raise PiMessageViewError('invalid_image')
    encoded = part['data']
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise PiMessageViewError('image_capacity')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise PiMessageViewError('invalid_image') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise PiMessageViewError('image_capacity')
    if not (raw.startswith(b'\x89PNG\r\n\x1a\n') if part['mimeType'] == 'image/png' else raw.startswith(b'\xff\xd8\xff')):
        raise PiMessageViewError('invalid_image')
    return {'data_base64': encoded, 'mime': part['mimeType'], 'size_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def _project(message):
    # Copy non-content fields separately so a multi-image data string is never
    # included in a second full message representation just for display.
    projected = {key: deepcopy(value) for key, value in message.items() if key != 'content'}
    content, changed = message.get('content'), False
    if type(content) is not list:
        if 'content' in message:
            projected['content'] = deepcopy(content)
        return projected, changed
    parts = []
    for part in content:
        if type(part) is not dict or part.get('type') != 'image':
            parts.append(deepcopy(part))
            continue
        changed = True
        item = {key: deepcopy(value) for key, value in part.items() if key != 'data'}
        item.update(display_omitted=True, original_sha256=None, original_bytes=None)
        try:
            image = _image(part)
            item.update(original_sha256=image['sha256'], original_bytes=image['size_bytes'])
        except PiMessageViewError as error:
            item.update(original_available=False, unavailable_reason=error.code)
        parts.append(item)
    projected['content'] = parts
    return projected, changed


def _placeholder(message, metadata):
    role = message.get('role')
    return {'type': 'rabbit_message_placeholder',
            'role': role if type(role) is str and len(role) <= 128 else 'unknown',
            'original_sha256': metadata['sha256'], 'original_size_bytes': metadata['size_bytes'],
            'reason': 'display_capacity'}


def _size(value):
    try:
        return canonical_metadata(value)['size_bytes']
    except PiMaterialProtocolError:
        raise PiMessageViewError('invalid_messages') from None


def project_messages(messages, before=None):
    _messages(messages)
    total = len(messages)
    if before is not None and (type(before) is not int or not 0 <= before <= total):
        raise PiMessageViewError('invalid_cursor')
    end = total if before is None else before
    rows, refs, row_bytes, ref_bytes = [], [], 0, 0
    start, projected_any = end, False

    def info(first, changed):
        return {'start': first, 'end': end, 'total': total, 'has_more_before': first > 0,
                'has_more_after': end < total, 'projected': changed}

    def page_size(first, changed, count, content_bytes, reference_bytes):
        empty = {'native_messages': [], 'native_message_refs': [], 'native_messages_info': info(first, changed)}
        return _size(empty) + content_bytes + reference_bytes + 2 * max(0, count - 1)

    for index in range(end - 1, max(-1, end - MAX_VIEW_MESSAGES - 1), -1):
        message = messages[index]
        metadata = _fingerprint(message)
        row, changed = _project(message)
        length = _size(row)
        reference = {'index': index, 'sha256': metadata['sha256'], 'projected': changed}
        reference_size = _size(reference)
        if page_size(index, changed, 1, length, reference_size) > MAX_VIEW_BYTES:
            row, changed = _placeholder(message, metadata), True
            length = _size(row)
            reference['projected'] = True
            reference_size = _size(reference)
        if page_size(index, projected_any or changed, len(rows) + 1, row_bytes + length, ref_bytes + reference_size) > MAX_VIEW_BYTES:
            if rows:
                break
            raise PiMessageViewError('view_capacity')
        rows.append(row); refs.append(reference)
        row_bytes += length; ref_bytes += reference_size
        projected_any |= changed
        start = index
    result = {'native_messages': list(reversed(rows)), 'native_message_refs': list(reversed(refs)),
              'native_messages_info': info(start, projected_any)}
    if page_size(start, projected_any, len(rows), row_bytes, ref_bytes) > MAX_VIEW_BYTES:
        raise PiMessageViewError('view_capacity')
    return result


def _original(messages, index, sha256):
    _messages(messages)
    if (type(index) is not int or not 0 <= index < len(messages)
            or type(sha256) is not str or _SHA.fullmatch(sha256) is None):
        raise PiMessageViewError('invalid_reference')
    message = messages[index]
    if _fingerprint(message)['sha256'] != sha256:
        raise PiMessageViewError('message_changed')
    return message


def original_message(messages, index, sha256):
    return deepcopy(_original(messages, index, sha256))


def original_image(messages, index, part_index, sha256):
    message = _original(messages, index, sha256)
    content = message.get('content')
    if (type(part_index) is not int or type(content) is not list
            or not 0 <= part_index < len(content)):
        raise PiMessageViewError('invalid_reference')
    return _image(content[part_index])
