"""Verified text files -> the original Hermes prompt, with no native I/O.

Hermes parses @references and scrubs internal/paste markers even inside JSON.
Only the host-owned material envelope is encoded defensively. Decoding its JSON
restores every source character; the user's own prompt keeps native semantics.
Images use verified host paths; this module never attaches them or calls RPC.
"""
import json
import re

from native_materials import CONTEXT_PREFIX, MAX_INPUT_BYTES, NativeMaterialError, validate_refs
from rabbit_runtime.native_session_store import request_sha256


MAX_USER_TEXT_BYTES = 128 * 1024
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_ESCAPES = {'@': r'\u0040', '<': r'\u003c', '>': r'\u003e',
            '[': r'\u005b', '~': r'\u007e'}


def validate_text_refs(refs):
    refs = validate_refs(refs)
    if any(ref['kind'] != 'text' for ref in refs):
        raise NativeMaterialError('images_unsupported')
    return refs


def material_json(value):
    """Escape native parser markers in JSON strings, never array syntax."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False)
    def protect(match):
        text = match.group()
        for char, escaped in _ESCAPES.items():
            text = text.replace(char, escaped)
        return text
    return _STRING.sub(protect, raw)


def prepare(message, refs, reader, *, image_publisher=None):
    try:
        if (type(message) is not str or not message.strip()
                or len(message.encode('utf-8')) > MAX_USER_TEXT_BYTES or not callable(reader)):
            raise NativeMaterialError('invalid_request')
        refs = validate_text_refs(refs) if image_publisher is None else validate_refs(refs)
        preview, originals = reader(refs)
        if preview['material_refs'] != refs or len(preview['images']) != len(originals):
            raise NativeMaterialError('material_changed')
        derived = message
        if refs:
            context = preview['context_text']
            if not context.startswith(CONTEXT_PREFIX):
                raise NativeMaterialError('material_changed')
            envelope = json.loads(context[len(CONTEXT_PREFIX):])
            derived += '\n\n' + CONTEXT_PREFIX + material_json(envelope)
        encoded = json.dumps([{'type': 'text', 'text': derived}], ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_INPUT_BYTES:
            raise NativeMaterialError('inputs_capacity')
        request = {'type': 'prompt', 'message': derived}
        images = []
        if preview['images']:
            if not callable(image_publisher):
                raise NativeMaterialError('images_unsupported')
            images = image_publisher(preview['images'], originals)
            if type(images) is not list or len(images) != len(originals):
                raise NativeMaterialError('material_changed')
            request['images'] = images
        return {'message': derived, 'request_sha256': request_sha256(request),
                'material_refs': refs, 'material_preview': preview,
                'input_size_bytes': len(encoded), **({'images': images} if images else {})}
    except NativeMaterialError:
        raise
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeMaterialError('invalid_request') from None
