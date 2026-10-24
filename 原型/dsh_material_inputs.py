"""Verified full UTF-8 files in one original DSH text input; no I/O or Agent.

DSH accepts text/image parts, not a general file object. File names, provenance
and complete contents therefore live in an explicit untrusted material JSON
suffix. The user's original question is the unchanged leading text.
"""
from copy import deepcopy
import hashlib
import json

from dsh_material_protocol import (DshMaterialProtocolError, MAX_USER_TEXT_BYTES,
                                   canonical_metadata, request)
from native_materials import (CONTEXT_PREFIX, MAX_TEXT_BYTES, NativeMaterialError,
                              validate_refs)


def _require(condition, code='material_changed'):
    if not condition:
        raise NativeMaterialError(code)


def validate_text_refs(refs):
    refs = validate_refs(refs)
    _require(all(ref['kind'] == 'text' for ref in refs), 'images_unsupported')
    return refs


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def prepare(message, refs, reader):
    """Return full final text, native request SHA and source preview metadata."""
    try:
        _require(type(message) is str and message.strip()
                 and len(message.encode('utf-8')) <= MAX_USER_TEXT_BYTES, 'invalid_request')
        refs = validate_text_refs(refs)
        preview, final = None, message
        if refs:
            _require(callable(reader), 'invalid_request')
            preview, originals = reader(deepcopy(refs))
            _require(type(preview) is dict and preview.get('material_refs') == refs
                     and preview.get('images') == [] and type(originals) is list and not originals
                     and type(preview.get('files')) is list and len(preview['files']) == len(refs))
            total = 0
            for ref, item in zip(refs, preview['files']):
                expected = {'id', 'name', 'sha256', 'size_bytes', 'source', 'text'}
                if 'source_chat_id' in ref:
                    expected.add('source_chat_id')
                _require(type(item) is dict and set(item) == expected
                         and item['id'] == ref['id'] and item['sha256'] == ref['sha256']
                         and item.get('source_chat_id') == ref.get('source_chat_id')
                         and type(item['name']) is str and item['source'] in ('upload', 'project_preview', 'generated')
                         and type(item['text']) is str and type(item['size_bytes']) is int)
                raw = item['text'].encode('utf-8')
                _require(len(raw) == item['size_bytes']
                         and hashlib.sha256(raw).hexdigest() == ref['sha256'])
                _require(len(raw) <= MAX_TEXT_BYTES, 'capacity')
                total += len(raw)
            for key, expected in (('size_bytes', total), ('text_size_bytes', total),
                                  ('image_size_bytes', 0), ('image_pixels', 0)):
                _require(type(preview.get(key)) is int and preview[key] == expected)
            envelope = {'schema_version': 1, 'kind': 'native_materials',
                        'images': [], 'files': preview['files']}
            context = CONTEXT_PREFIX + _json(envelope)
            _require(preview.get('context_text') == context)
            final += '\n\n' + context
        native_request = request(final, original_message=message)
        metadata = canonical_metadata(native_request)
        return {'message': final, 'request': native_request,
                'request_sha256': metadata['sha256'], 'input_size_bytes': metadata['size_bytes'],
                'material_refs': refs, 'material_preview': deepcopy(preview)}
    except NativeMaterialError:
        raise
    except DshMaterialProtocolError as error:
        raise NativeMaterialError('inputs_capacity' if error.code == 'capacity' else 'invalid_request') from None
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise NativeMaterialError('invalid_request') from None
