"""Verified host materials -> original Pi prompt fields; no native execution.

The reader owns source identity, full bytes and version checks. This adapter
owns Pi's text/base64 shape and separate text/wire budgets. It publishes no
image cache and never writes a transcript or changes a model configuration.
"""
import base64
import json

from native_materials import NativeMaterialError, MAX_INPUT_BYTES, validate_refs


MAX_USER_TEXT_BYTES = 128 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def prepare(message, refs, reader):
    try:
        if (type(message) is not str or not message.strip()
                or len(message.encode('utf-8')) > MAX_USER_TEXT_BYTES or not callable(reader)):
            raise NativeMaterialError('invalid_request')
        refs = validate_refs(refs)
        preview, originals = reader(refs)
        if preview['material_refs'] != refs or len(originals) != len(preview['images']):
            raise NativeMaterialError('material_changed')
        derived = message + ('\n\n' + preview['context_text'] if refs else '')
        # Like Codex, admit the entire JSON text input before encoding images.
        if len(_json([{'type': 'text', 'text': derived}]).encode('utf-8')) > MAX_INPUT_BYTES:
            raise NativeMaterialError('inputs_capacity')
        images = [{'type': 'image', 'data': base64.b64encode(raw).decode('ascii'), 'mimeType': image['mime']}
                  for image, raw in zip(preview['images'], originals)]
        request = {'type': 'prompt', 'message': derived}
        if images:
            request['images'] = images
        from rabbit_runtime.pi.material_protocol import request_sha256
        digest = request_sha256(request)
        return {'message': derived, 'images': images, 'request_sha256': digest,
                'material_refs': refs, 'material_preview': preview,
                'input_size_bytes': len(_json(request).encode('utf-8'))}
    except NativeMaterialError:
        raise
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeMaterialError('invalid_request') from None
