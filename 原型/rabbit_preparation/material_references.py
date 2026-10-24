"""Pure, bounded material references; no storage access or send authority."""
import re
import uuid

from rabbit_content.materials import valid_id


MAX_IMAGES = MAX_FILES = 4
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class MaterialReferenceError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_request', 'capacity') else 'invalid_request'
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise MaterialReferenceError(code)


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_material_refs(refs):
    """Copy exact finite references in order, without opening their sources."""
    _require(type(refs) is list and len(refs) <= MAX_IMAGES + MAX_FILES, 'invalid_request')
    result, seen = [], set()
    images = files = 0
    for ref in refs:
        _require(type(ref) is dict and ref.get('kind') in ('image', 'text'), 'invalid_request')
        kind = ref['kind']
        required = {'kind', 'id', 'sha256'}
        _require(required <= ref.keys() and not ref.keys() - (required | ({'source_chat_id'} if kind == 'text' else set())),
                 'invalid_request')
        _require(type(ref['sha256']) is str and _SHA.fullmatch(ref['sha256']) is not None, 'invalid_request')
        _require(type(ref['id']) is str and (valid_id(ref['id']) if kind == 'image' else _uuid(ref['id'])), 'invalid_request')
        _require('source_chat_id' not in ref or _uuid(ref['source_chat_id']), 'invalid_request')
        identity = kind, ref['id']
        _require(identity not in seen, 'invalid_request')
        seen.add(identity)
        images += kind == 'image'; files += kind == 'text'
        _require(images <= MAX_IMAGES and files <= MAX_FILES, 'capacity')
        result.append(dict(ref))
    return result
