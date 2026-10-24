"""Validate a host-prepared text snapshot without reading files or paths."""
import hashlib
import re
import uuid


MAX_TEXT_FILE_BYTES = 512 * 1024
MAX_MESSAGE_TEXT_FILES = 4
SOURCES = frozenset(('upload', 'project_preview', 'generated'))
MISSING_REASONS = frozenset(('not_found', 'changed', 'unverified', 'preparation_capacity'))
_MATERIAL_KEYS = frozenset(('id', 'name', 'sha256', 'size_bytes', 'source', 'text'))
_MISSING_KEYS = frozenset(('id', 'sha256', 'name', 'size_bytes', 'source', 'reason'))


def _identity(value):
    if not isinstance(value, str):
        raise ValueError('上下文中的文本文件标识无效。')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError('上下文中的文本文件标识无效。') from None
    if str(parsed) != value:
        raise ValueError('上下文中的文本文件标识无效。')


def _metadata(item):
    _identity(item.get('id'))
    if not isinstance(item.get('sha256'), str) or re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None:
        raise ValueError('上下文中的文本文件版本无效。')
    if 'name' in item:
        name = item['name']
        if not isinstance(name, str) or not name:
            raise ValueError('上下文中的文本文件名称无效。')
        try:
            encoded = name.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('上下文中的文本文件名称无效。') from None
        if len(encoded) > 255:
            raise ValueError('上下文中的文本文件名称无效。')
    if 'size_bytes' in item and (type(item['size_bytes']) is not int
                               or not 0 <= item['size_bytes'] <= MAX_TEXT_FILE_BYTES):
        raise ValueError('上下文中的文本文件大小无效。')
    if 'source' in item and (not isinstance(item['source'], str) or item['source'] not in SOURCES):
        raise ValueError('上下文中的文本文件来源无效。')


def validate_materials(materials=None, missing=None):
    """Return detached DTOs; the four-item limit belongs to one message only."""
    materials = [] if materials is None else materials
    missing = [] if missing is None else missing
    if not isinstance(materials, list) or not isinstance(missing, list):
        raise ValueError('上下文中的文本文件资料无效。')
    if len(materials) + len(missing) > MAX_MESSAGE_TEXT_FILES:
        raise ValueError('一条消息最多附带四份不同的文本文件。')
    ready, unavailable, seen = [], [], set()
    for item in materials:
        if not isinstance(item, dict) or set(item) != _MATERIAL_KEYS:
            raise ValueError('上下文中的文本文件资料无效。')
        _metadata(item)
        if not isinstance(item['text'], str):
            raise ValueError('上下文中的文本文件正文无效。')
        try:
            raw = item['text'].encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('上下文中的文本文件不是完整 UTF-8 文字。') from None
        if (len(raw) > MAX_TEXT_FILE_BYTES or len(raw) != item['size_bytes']
                or hashlib.sha256(raw).hexdigest() != item['sha256']):
            raise ValueError('上下文中的文本文件正文与大小或版本不一致。')
        if item['id'] in seen:
            raise ValueError('一条消息不能重复附带同一份文本文件。')
        seen.add(item['id'])
        ready.append(dict(item))
    for item in missing:
        if (not isinstance(item, dict) or not {'id', 'sha256', 'reason'} <= set(item)
                or not set(item) <= _MISSING_KEYS
                or not isinstance(item['reason'], str) or item['reason'] not in MISSING_REASONS):
            raise ValueError('上下文中的缺失文本文件资料无效。')
        _metadata(item)
        if item['id'] in seen:
            raise ValueError('一条消息不能重复附带同一份文本文件。')
        seen.add(item['id'])
        unavailable.append(dict(item))
    return ready, unavailable
