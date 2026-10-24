"""Verified shared materials -> one Claude SDK user frame; no Agent or I/O.

The shared reader owns source access and immutable source-version checks.
This adapter verifies its complete return, preserves the user's native final
text block, and gives the worker a separate, bounded material packet.
"""
import base64
from copy import deepcopy
import hashlib
import json

from rabbit_runtime.claude.material_protocol import (
    ClaudeMaterialProtocolError, MAX_INPUT_TEXT_BYTES, MAX_REQUEST_BYTES,
    MAX_USER_TEXT_BYTES, canonical_metadata, request as material_request,
    user_content,
)
from rabbit_content.materials import MAX_FILE_BYTES, PUBLIC_FIELDS
from native_materials import (
    CONTEXT_PREFIX, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MAX_TEXT_BYTES,
    NativeMaterialError, validate_refs,
)
from rabbit_runtime.native_session_store import NativeSessionStoreError, request_sha256


def _require(condition, code='material_changed'):
    if not condition:
        raise NativeMaterialError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _read(refs, reader):
    """Check complete original bytes and their ordered source identities."""
    preview, originals = reader(deepcopy(refs))
    _require(type(preview) is dict and type(originals) is list
             and preview.get('material_refs') == refs
             and type(preview.get('images')) is list
             and type(preview.get('files')) is list)
    image_refs = [ref for ref in refs if ref['kind'] == 'image']
    file_refs = [ref for ref in refs if ref['kind'] == 'text']
    _require(len(preview['images']) == len(originals) == len(image_refs)
             and len(preview['files']) == len(file_refs))
    images, image_bytes, pixels, text_bytes = [], 0, 0, 0
    for ref, image, raw in zip(image_refs, preview['images'], originals):
        _require(type(image) is dict and set(image) == set((*PUBLIC_FIELDS, 'sha256'))
                 and image['id'] == ref['id'] and image['sha256'] == ref['sha256']
                 and image['mime'] in ('image/png', 'image/jpeg')
                 and type(raw) is bytes and type(image['size_bytes']) is int
                 and 0 < len(raw) == image['size_bytes'] <= MAX_FILE_BYTES
                 and hashlib.sha256(raw).hexdigest() == ref['sha256']
                 and type(image['width']) is int and image['width'] > 0
                 and type(image['height']) is int and image['height'] > 0)
        _require(raw.startswith(b'\x89PNG\r\n\x1a\n') if image['mime'] == 'image/png'
                 else raw.startswith(b'\xff\xd8\xff'))
        images.append({'data_base64': base64.b64encode(raw).decode('ascii'),
                       'mime': image['mime'], 'size_bytes': len(raw),
                       'sha256': image['sha256']})
        image_bytes += len(raw)
        pixels += image['width'] * image['height']
    _require(image_bytes <= MAX_IMAGE_BYTES and pixels <= MAX_IMAGE_PIXELS, 'capacity')
    for ref, item in zip(file_refs, preview['files']):
        _require(type(item) is dict
                 and set(item) == {'id', 'name', 'sha256', 'size_bytes', 'source', 'text'}
                     | ({'source_chat_id'} if 'source_chat_id' in ref else set())
                 and item['id'] == ref['id'] and item['sha256'] == ref['sha256']
                 and item.get('source_chat_id') == ref.get('source_chat_id')
                 and type(item['text']) is str and type(item['size_bytes']) is int)
        raw = item['text'].encode('utf-8', 'strict')
        _require(len(raw) == item['size_bytes']
                 and hashlib.sha256(raw).hexdigest() == ref['sha256'])
        _require(len(raw) <= MAX_TEXT_BYTES, 'capacity')
        text_bytes += len(raw)
    for key, expected in (('image_size_bytes', image_bytes), ('text_size_bytes', text_bytes),
                          ('size_bytes', image_bytes + text_bytes), ('image_pixels', pixels)):
        _require(type(preview.get(key)) is int and preview[key] == expected)
    # Use the reader's context verbatim. This checks its source-envelope
    # agreement without extracting, rewriting, or parsing the user's prompt.
    envelope = {'schema_version': 1, 'kind': 'native_materials',
                'images': preview['images'], 'files': preview['files']}
    _require(preview.get('context_text') == CONTEXT_PREFIX + _json(envelope))
    return preview, images


def prepare(message, refs, reader):
    """Return a small hash request plus a worker-only original-image packet."""
    try:
        _require(type(message) is str and message.strip()
                 and len(message.encode('utf-8', 'strict')) <= MAX_USER_TEXT_BYTES,
                 'invalid_request')
        refs = validate_refs(refs)
        if not refs:
            # Preserve the original string submission and hash exactly. Even a
            # source reader without usable credentials must not be invoked.
            request = {'type': 'prompt', 'message': message}
            return {'message': message, 'material_input': None, 'request': request,
                    'request_sha256': request_sha256(request), 'material_refs': [],
                    'material_preview': None,
                    'input_size_bytes': len(_json(request).encode('utf-8'))}
        _require(callable(reader), 'invalid_request')
        preview, images = _read(refs, reader)
        context = preview['context_text']
        canonical_metadata([{'type': 'text', 'text': context}, {'type': 'text', 'text': message}],
                           max_bytes=MAX_INPUT_TEXT_BYTES)
        metadata = [{key: image[key] for key in ('sha256', 'mime', 'size_bytes')}
                    for image in images]
        request = material_request(message, context, metadata)
        digest = request_sha256(request)
        packet = {'schema_version': 1, 'context_text': context,
                  'images': images, 'request_sha256': digest}
        content = user_content(message, packet)
        size = canonical_metadata(content, max_bytes=MAX_REQUEST_BYTES)['size_bytes']
        return {'message': message, 'material_input': packet, 'request': request,
                'request_sha256': digest, 'material_refs': refs,
                'material_preview': deepcopy(preview), 'input_size_bytes': size}
    except NativeMaterialError:
        raise
    except ClaudeMaterialProtocolError as error:
        raise NativeMaterialError('inputs_capacity' if error.code == 'capacity' else 'material_changed') from None
    except (NativeSessionStoreError, KeyError, ValueError, TypeError, UnicodeError,
            RecursionError, OverflowError):
        raise NativeMaterialError('invalid_request') from None
