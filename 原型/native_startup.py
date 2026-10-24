"""Finite native startup settings; stdlib only, no Agent or service startup.

Executable paths retain their lexical entry, including a trusted venv symlink.
This codec never accepts credentials, environment overrides or arbitrary argv.
"""
import json
import math
import os
from pathlib import Path
import stat


MAX_BYTES = 64 * 1024
MAX_PATH_CHARS = 8192
CONNECTIONS = ('codex', 'pi', 'hermes', 'claude', 'dsh', 'specialist')
_FIELDS = {
    'pi': (('node', 'cli'), ()),
    'hermes': (('node', 'python', 'root'), ('home', 'session_settings')),
    'claude': (('python', 'cli'), ()),
    'dsh': (('node', 'cli', 'root'), ('home', 'history_root')),
    'specialist': (('mcp_python',), ('codex_cli', 'work_document')),
}
_EXECUTABLES = {
    ('codex', 'cli'), ('codex', 'management_python'), ('pi', 'node'),
    ('hermes', 'node'), ('hermes', 'python'), ('claude', 'python'),
    ('claude', 'cli'), ('dsh', 'node'), ('specialist', 'mcp_python'), ('specialist', 'codex_cli'),
}
_FLAGS = {
    'codex': {'cli': '--native-codex-cli', 'management_python': '--native-chat-mcp-python'},
    'pi': {'node': '--native-pi-node', 'cli': '--native-pi-cli'},
    'hermes': {'node': '--native-hermes-node', 'python': '--native-hermes-python',
               'root': '--native-hermes-root', 'home': '--native-hermes-home',
               'session_settings': '--native-hermes-session-settings'},
    'claude': {'python': '--native-claude-python', 'cli': '--native-claude-cli'},
    'dsh': {'node': '--native-dsh-node', 'cli': '--native-dsh-cli', 'root': '--native-dsh-root',
            'home': '--native-dsh-home', 'history_root': '--native-dsh-history-root'},
    'specialist': {'mcp_python': '--specialist-mcp-python', 'codex_cli': '--specialist-codex-cli',
                   'work_document': '--specialist-work-document'},
}


class NativeStartupError(ValueError):
    """Safe bounded error; neither input values nor private paths are echoed."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('原生启动配置无效（' + reason + '）。')


def _reject(reason):
    raise NativeStartupError(reason)


def _path(value, *, kind, check_paths):
    if (type(value) is not str or not value or len(value) > MAX_PATH_CHARS
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _reject('invalid_path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        _reject('invalid_path')
    if check_paths:
        try:
            mode = path.stat().st_mode
            regular = stat.S_ISDIR(mode) if kind == 'directory' else stat.S_ISREG(mode)
            access = os.R_OK | (os.X_OK if kind in ('directory', 'executable') else 0)
            if not regular or not os.access(path, access):
                _reject('path_unavailable')
        except (OSError, ValueError):
            _reject('path_unavailable')
    # Path removes redundant separators and '.' without resolving symlinks.
    return str(path)


def normalize(value, check_paths=True):
    """Return a fresh canonical schema1 object; optionally check local paths.

    check_paths=False validates saved snapshot structure only. It provides no
    evidence that a referenced binary, package or home still exists.
    """
    if type(check_paths) is not bool:
        _reject('invalid_check_paths')
    if type(value) is not dict or set(value) != {'schema_version', 'connections'}:
        _reject('invalid_document')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        _reject('unsupported_schema')
    connections = value['connections']
    if type(connections) is not dict or any(type(k) is not str or k not in CONNECTIONS for k in connections):
        _reject('invalid_connections')
    out = {}
    for provider in CONNECTIONS:
        if provider not in connections:
            continue
        entry = connections[provider]
        if type(entry) is not dict:
            _reject('invalid_connection')
        if provider == 'codex':
            if set(entry) not in ({'cli'}, {'management_python'}):
                _reject('invalid_codex_connection')
            fields = ('cli',) if 'cli' in entry else ('management_python',)
        else:
            required, optional = _FIELDS[provider]
            if not set(required) <= entry.keys() or entry.keys() - set(required + optional):
                _reject('invalid_connection_fields')
            fields = required + tuple(k for k in optional if k in entry)
        normalized = {}
        for field in fields:
            raw = entry[field]
            if field in ('session_settings', 'work_document'):
                if type(raw) is not bool:
                    _reject('invalid_' + field)
                normalized[field] = raw
            else:
                kind = ('directory' if field in ('root', 'home', 'history_root') else
                        'executable' if (provider, field) in _EXECUTABLES else 'file')
                normalized[field] = _path(raw, kind=kind, check_paths=check_paths)
        out[provider] = normalized
    return {'schema_version': 1, 'connections': out}


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _reject('duplicate_key')
        out[key] = value
    return out


def _constant(_value):
    _reject('nonfinite_json')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        _reject('nonfinite_json')
    return number


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def load(path):
    """Read one bounded ordinary non-symlink JSON file, then validate paths."""
    if isinstance(path, Path):
        path = str(path)
    location = _path(path, kind='file', check_paths=False)
    descriptor = None
    try:
        named_before = os.stat(location, follow_symlinks=False)
        if not stat.S_ISREG(named_before.st_mode) or named_before.st_size > MAX_BYTES:
            _reject('invalid_config_file')
        descriptor = os.open(location, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES or _identity(named_before) != _identity(before):
            _reject('invalid_config_file')
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            chunk = os.read(descriptor, min(16384, MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(location, follow_symlinks=False)
        if (len(raw) > MAX_BYTES or not stat.S_ISREG(named_after.st_mode)
                or _identity(before) != _identity(after) or _identity(after) != _identity(named_after)):
            _reject('config_file_changed')
    except OSError:
        _reject('config_file_unavailable')
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        value = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float)
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, NativeStartupError):
            raise
        _reject('invalid_json')
    return normalize(value)


def arguments(config, check_paths=True):
    """Generate only the server's fixed startup flags, in canonical order."""
    normalized = normalize(config, check_paths=check_paths)
    argv = []
    for provider, entry in normalized['connections'].items():
        for field, value in entry.items():
            flag = _FLAGS[provider][field]
            if field in ('session_settings', 'work_document'):
                if value:
                    argv.append(flag)
            else:
                argv.extend((flag, value))
    return argv
