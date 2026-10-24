"""One small identity table shared by the host and browser.

Identity is distinct from capability. This module reads no native settings,
credentials or processes; each adapter owns its actual availability and routes.
"""
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType


def _load():
    path = Path(__file__).parent / 'agent_definitions.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError('Agent身份定义不是有界普通文件。')
        with os.fdopen(os.dup(fd), 'rb') as handle:
            raw = handle.read(8193)
        rows = json.loads(raw)
    finally:
        os.close(fd)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError('Agent身份定义无效。')
    seen, result = set(), []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'id', 'label', 'initial'}
                or not isinstance(row['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', row['id'])
                or row['id'] in seen or row['id'] in {'constructor', 'prototype', '__proto__'}
                or not isinstance(row['label'], str) or not 1 <= len(row['label']) <= 60
                or not row['label'].strip()
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in row['label'])
                or not isinstance(row['initial'], str) or not re.fullmatch(r'[A-Za-z0-9]{1,3}', row['initial'])):
            raise ValueError('Agent身份定义无效。')
        seen.add(row['id'])
        result.append(MappingProxyType(dict(row)))
    return tuple(result)


ENTRIES = _load()
IDS = tuple(row['id'] for row in ENTRIES)
LABELS = MappingProxyType({row['id']: row['label'] for row in ENTRIES})
INSTANCES = frozenset('local/' + agent for agent in IDS)
_MODEL = re.compile(r'local/([^/]+)/[^\x00-\x20\x7f]{1,192}\Z')


def has(value):
    return isinstance(value, str) and value in LABELS


def instance_id(value):
    return 'local/' + value if has(value) else None


def backend_of_instance(value):
    return value[6:] if isinstance(value, str) and value in INSTANCES else None


def valid_model_id(value, backend=None):
    if not isinstance(value, str) or len(value) > 220 or (backend is not None and not has(backend)):
        return False
    if value == '':
        return True
    match = _MODEL.fullmatch(value)
    return bool(match and has(match[1]) and (backend is None or match[1] == backend))


def empty_models():
    return {agent: '' for agent in IDS}


def browser_definitions():
    """Only the host's validated identity data is inserted before agents.js."""
    text = json.dumps([dict(row) for row in ENTRIES], ensure_ascii=False, separators=(',', ':'))
    text = text.replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return 'window.RabbitAgentDefinitions=' + text + ';\n'
