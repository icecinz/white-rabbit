"""Claude material transport bounds and original SDK user-frame validation.

Only the final text block goes through Claude CLI's native @/command parser.
Quoted material precedes that block. Native hooks still see all text blocks;
this is explicit material input, not a claim of identical hook string input.
"""
import base64
import binascii
import hashlib
import re

from ..native_json import canonical_metadata as json_metadata, NativeJSONError
from ..native_session_store import request_sha256 as small_request_sha256, NativeSessionStoreError


MAX_INPUT_TEXT_BYTES = 900 * 1024
MAX_USER_TEXT_BYTES = 128 * 1024
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_RPC_FRAME_BYTES = 64 * 1024 * 1024
MAX_RPC_EVENT_BYTES = 128 * 1024 * 1024
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_SESSION_LINE_BYTES = 64 * 1024 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r'[a-f0-9]{64}\Z')


class ClaudeMaterialProtocolError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_json', 'capacity', 'invalid_material') else 'invalid_json'
        super().__init__('Claude 原生材料或记录超出容量。' if self.code == 'capacity' else 'Claude 原生材料或记录无法完整核验。')


def canonical_metadata(value, *, max_bytes=MAX_RECORD_BYTES):
    try:
        return json_metadata(value, max_bytes=max_bytes, max_depth=256, max_nodes=2000000)
    except NativeJSONError as error:
        raise ClaudeMaterialProtocolError(error.code) from None


def _text(value, limit):
    if type(value) is not str or not value.strip() or len(value.encode('utf-8', 'strict')) > limit:
        raise ClaudeMaterialProtocolError('invalid_material')
    return value


def _image(value):
    if (type(value) is not dict or set(value) != {'data_base64', 'mime', 'size_bytes', 'sha256'}
            or value.get('mime') not in ('image/png', 'image/jpeg')
            or type(value.get('sha256')) is not str or _SHA.fullmatch(value['sha256']) is None
            or type(value.get('size_bytes')) is not int or not 1 <= value['size_bytes'] <= MAX_IMAGE_BYTES
            or type(value.get('data_base64')) is not str
            or len(value['data_base64']) != 4 * ((value['size_bytes'] + 2) // 3)):
        raise ClaudeMaterialProtocolError('invalid_material')
    try:
        raw = base64.b64decode(value['data_base64'], validate=True)
    except (ValueError, binascii.Error):
        raise ClaudeMaterialProtocolError('invalid_material') from None
    if (len(raw) != value['size_bytes'] or base64.b64encode(raw).decode('ascii') != value['data_base64']
            or hashlib.sha256(raw).hexdigest() != value['sha256']
            or not (raw.startswith(b'\x89PNG\r\n\x1a\n') if value['mime'] == 'image/png' else raw.startswith(b'\xff\xd8\xff'))):
        raise ClaudeMaterialProtocolError('invalid_material')
    return {'sha256': value['sha256'], 'mime': value['mime'], 'size_bytes': len(raw)}


def request(message, context_text, images):
    """Exact small derived request; images contain metadata, never pixels."""
    try:
        _text(message, MAX_USER_TEXT_BYTES); _text(context_text, MAX_INPUT_TEXT_BYTES)
        if (type(images) is not list or len(images) > MAX_IMAGES
                or any(type(image) is not dict or set(image) != {'sha256', 'mime', 'size_bytes'}
                    or type(image['sha256']) is not str or _SHA.fullmatch(image['sha256']) is None
                    or image['mime'] not in ('image/png', 'image/jpeg')
                    or type(image['size_bytes']) is not int or not 1 <= image['size_bytes'] <= MAX_IMAGE_BYTES for image in images)
                or sum(image['size_bytes'] for image in images) > MAX_IMAGE_TOTAL_BYTES):
            raise ClaudeMaterialProtocolError('invalid_material')
        result = {'type': 'prompt', 'message': message, 'context_text': context_text}
        if images:
            result['images'] = [dict(image) for image in images]
        # Count JSON escaping, both text blocks and per-block shape together.
        canonical_metadata([{'type': 'text', 'text': context_text}, {'type': 'text', 'text': message}],
                           max_bytes=MAX_INPUT_TEXT_BYTES)
        small_request_sha256(result)
        return result
    except ClaudeMaterialProtocolError:
        raise
    except (NativeSessionStoreError, UnicodeError, TypeError, ValueError, KeyError):
        raise ClaudeMaterialProtocolError('invalid_material') from None


def user_content(message, material_input):
    """Validate a trusted host packet before emitting one original SDK frame."""
    try:
        if (type(material_input) is not dict
                or set(material_input) != {'schema_version', 'context_text', 'images', 'request_sha256'}
                or type(material_input['schema_version']) is not int or material_input['schema_version'] != 1
                or type(material_input['request_sha256']) is not str or _SHA.fullmatch(material_input['request_sha256']) is None
                or type(material_input['images']) is not list or len(material_input['images']) > MAX_IMAGES):
            raise ClaudeMaterialProtocolError('invalid_material')
        metadata = [_image(image) for image in material_input['images']]
        derived = request(message, material_input['context_text'], metadata)
        if material_input['request_sha256'] != small_request_sha256(derived):
            raise ClaudeMaterialProtocolError('invalid_material')
        content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': image['mime'], 'data': image['data_base64']}}
                   for image in material_input['images']]
        content.extend([{'type': 'text', 'text': material_input['context_text']}, {'type': 'text', 'text': message}])
        canonical_metadata(content, max_bytes=MAX_REQUEST_BYTES)
        return content
    except ClaudeMaterialProtocolError:
        raise
    except (NativeSessionStoreError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise ClaudeMaterialProtocolError('invalid_material') from None
