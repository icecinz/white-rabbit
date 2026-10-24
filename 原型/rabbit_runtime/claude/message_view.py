"""Finite display pages over official Claude SDK records, with exact originals.

The caller owns the session, SDK offset and file identity. This module performs
no source or Agent I/O, reconstructs no transcript and never joins text blocks.
Fingerprints describe the complete canonical SDK wrapper, before projection.
"""
import base64
import binascii
from copy import deepcopy
import hashlib
import re

from . import material_protocol as protocol


MAX_VIEW_RECORDS = 200
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
_MAX_OFFSET = 2**53 - 1
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MIMES = frozenset(('image/png', 'image/jpeg'))


class ClaudeMessageViewError(ValueError):
    def __init__(self, code):
        self.code = code if code in (
            'invalid_records', 'invalid_cursor', 'invalid_reference', 'record_changed',
            'invalid_image', 'unsupported_image', 'nested_image_unsupported',
            'image_capacity', 'view_capacity') else 'invalid_records'
        descriptions = {
            'record_changed': 'Claude SDK 显示记录版本已变化，请刷新后重新读取。',
            'image_capacity': 'Claude 原图超过本次读取容量。',
            'view_capacity': 'Claude SDK 显示记录超过本页容量，原记录未修改。',
            'unsupported_image': '这份 Claude SDK 记录没有受支持的嵌入原图。',
        }
        super().__init__(descriptions.get(self.code, 'Claude SDK 显示记录或原图引用无法核验。'))


def _offset(value):
    if type(value) is not int or not 0 <= value <= _MAX_OFFSET:
        raise ClaudeMessageViewError('invalid_cursor')
    return value


def _metadata(value):
    try:
        return protocol.canonical_metadata(value, max_bytes=protocol.MAX_RECORD_BYTES)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ClaudeMessageViewError('invalid_records') from None


def _record(record):
    if (type(record) is not dict or type(record.get('sdk_type')) is not str
            or not 0 < len(record['sdk_type']) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in record['sdk_type'])
            or type(record.get('data')) is not dict):
        raise ClaudeMessageViewError('invalid_records')
    value = record['data'].get('uuid')
    if value is not None and (type(value) is not str or not 0 < len(value) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ClaudeMessageViewError('invalid_records')
    return _metadata(record)


def _content(record):
    data = record['data']
    message = data.get('message')
    if type(message) is dict and 'content' in message:
        return message['content']
    return data.get('content')


def _image(part):
    if type(part) is not dict or part.get('type') != 'image':
        raise ClaudeMessageViewError('invalid_image')
    source = part.get('source')
    if (type(source) is not dict or source.get('type') != 'base64'
            or type(source.get('media_type')) is not str
            or source.get('media_type') not in _MIMES):
        raise ClaudeMessageViewError('unsupported_image')
    encoded = source.get('data')
    if type(encoded) is not str or not encoded:
        raise ClaudeMessageViewError('invalid_image')
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ClaudeMessageViewError('image_capacity')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise ClaudeMessageViewError('invalid_image') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ClaudeMessageViewError('image_capacity')
    mime = source['media_type']
    if not (raw.startswith(b'\x89PNG\r\n\x1a\n') if mime == 'image/png'
            else raw.startswith(b'\xff\xd8\xff')):
        raise ClaudeMessageViewError('invalid_image')
    return {'data_base64': encoded, 'mime': mime, 'size_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def _project(record):
    content = _content(record)
    direct = {}
    if type(content) is list:
        number = 0
        for index, part in enumerate(content):
            if type(part) is dict and part.get('type') == 'image':
                direct[index] = (index, number)
                number += 1
    changed = False

    def walk(value, position=None):
        nonlocal changed
        if type(value) is list:
            return [walk(item, direct.get(index) if value is content else None)
                    for index, item in enumerate(value)]
        if type(value) is not dict:
            return value
        if value.get('type') != 'image':
            return {key: walk(item) for key, item in value.items()}
        changed = True
        # Native image bytes stay exclusively in the original SDK record.
        result = {key: walk(item) for key, item in value.items() if key != 'source'}
        source = value.get('source')
        if type(source) is dict:
            result['source'] = {key: walk(item) for key, item in source.items() if key != 'data'}
        elif 'source' in value:
            result['source'] = None
        result.update(display_omitted=True, part_index=position[0] if position else None)
        try:
            if position is None:
                raise ClaudeMessageViewError('nested_image_unsupported')
            if position[1] >= MAX_IMAGES:
                raise ClaudeMessageViewError('image_capacity')
            image = _image(value)
            result.update(mime=image['mime'], size_bytes=image['size_bytes'],
                          sha256=image['sha256'], original_available=True)
        except ClaudeMessageViewError as error:
            result.update(original_available=False, unavailable_reason=error.code)
        return result

    return walk(record), changed


def _placeholder(record, metadata):
    original = record['data']
    data = {'display_placeholder': True, 'uuid': original.get('uuid'),
            'original_sha256': metadata['sha256'], 'original_size_bytes': metadata['size_bytes'],
            'reason': 'display_capacity'}
    for key in ('type', 'session_id'):
        value = original.get(key)
        if type(value) is str and len(value) <= 256:
            data[key] = value
    return {'sdk_type': record['sdk_type'], 'data': data}


def project_records(records, *, offset, has_more=False):
    """Project one supplied SDK window; never load or infer the next window."""
    _offset(offset)
    if (type(records) is not list or type(has_more) is not bool
            or offset + min(len(records), MAX_VIEW_RECORDS) > _MAX_OFFSET
            or not records and has_more):
        raise ClaudeMessageViewError('invalid_records')
    rows, refs, row_bytes, ref_bytes, projected_any = [], [], 0, 0, False

    def info(count, changed):
        more = has_more or count < len(records)
        return {'offset': offset, 'end_offset': offset + count, 'has_more': more,
                'next_offset': offset + count if more else None, 'projected': changed}

    def page_size(count, changed, content_bytes, reference_bytes):
        empty = {'native_records': [], 'native_record_refs': [],
                 'native_records_info': info(count, changed)}
        return _metadata(empty)['size_bytes'] + content_bytes + reference_bytes + 2 * max(0, count - 1)

    for index, record in enumerate(records[:MAX_VIEW_RECORDS]):
        metadata = _record(record)
        row, changed = _project(record)
        size = _metadata(row)['size_bytes']
        reference = {'offset': offset + index, 'uuid': record['data'].get('uuid'),
                     'sha256': metadata['sha256'], 'projected': changed}
        reference_size = _metadata(reference)['size_bytes']
        if page_size(1, changed, size, reference_size) > protocol.MAX_VIEW_BYTES:
            row, changed = _placeholder(record, metadata), True
            reference['projected'] = True
            size, reference_size = _metadata(row)['size_bytes'], _metadata(reference)['size_bytes']
        if page_size(len(rows) + 1, projected_any or changed, row_bytes + size,
                     ref_bytes + reference_size) > protocol.MAX_VIEW_BYTES:
            if rows:
                break
            raise ClaudeMessageViewError('view_capacity')
        rows.append(row); refs.append(reference)
        row_bytes += size; ref_bytes += reference_size
        projected_any |= changed
    result = {'native_records': rows, 'native_record_refs': refs,
              'native_records_info': info(len(rows), projected_any)}
    if page_size(len(rows), projected_any, row_bytes, ref_bytes) > protocol.MAX_VIEW_BYTES:
        raise ClaudeMessageViewError('view_capacity')
    return result


def _original(record, ref):
    if (type(ref) is not dict or set(ref) != {'offset', 'uuid', 'sha256', 'projected'}
            or type(ref.get('offset')) is not int or not 0 <= ref['offset'] <= _MAX_OFFSET
            or ref.get('uuid') is not None and (type(ref['uuid']) is not str
                or not 0 < len(ref['uuid']) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in ref['uuid']))
            or type(ref.get('sha256')) is not str or _SHA.fullmatch(ref['sha256']) is None
            or type(ref.get('projected')) is not bool):
        raise ClaudeMessageViewError('invalid_reference')
    metadata = _record(record)
    if record['data'].get('uuid') != ref['uuid'] or metadata['sha256'] != ref['sha256']:
        raise ClaudeMessageViewError('record_changed')
    return record


def original_record(record, ref):
    """Verify the supplied exact SDK record; offset ownership is the caller's."""
    return deepcopy(_original(record, ref))


def original_image(record, ref, part_index):
    record = _original(record, ref)
    content = _content(record)
    if (type(part_index) is not int or type(content) is not list
            or not 0 <= part_index < len(content)):
        raise ClaudeMessageViewError('invalid_reference')
    if sum(type(part) is dict and part.get('type') == 'image'
           for part in content[:part_index + 1]) > MAX_IMAGES:
        raise ClaudeMessageViewError('image_capacity')
    return _image(content[part_index])
