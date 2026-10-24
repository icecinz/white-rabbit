"""Immutable local links to a fixed, existing Hermes Hindsight source.

This module does no network I/O, reads no native configuration or bank contents,
and never provisions or changes a physical bank. ``attach`` is an internal
server operation after the caller has verified the configured native source.
Its profile retains the normal logical Rabbit namespace; only this private
table records the different physical, read-only bank. Public snapshots remain
the nine fields from ``memory_profiles`` and contain no endpoint or bank ID.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from .memory_profiles import (MemoryProfiles, initialize_schema as initialize_profiles,
                             validate_memory_profile_fields, validate_memory_profile_id)


KIND = 'hermes_hindsight_readonly'
SOURCE_FIELDS = ('kind', 'api_url', 'native_bank_id', 'source_fingerprint')
BINDING_COLUMNS = ('profile_id',) + SOURCE_FIELDS + ('created_at',)


def make_native_source(api_url, native_bank_id):
    """Canonical private source identity; syntax only, never service availability."""
    if (not isinstance(api_url, str) or not 1 <= len(api_url) <= 200
            or any(ord(char) <= 32 or ord(char) == 127 for char in api_url)):
        raise ValueError('原有记忆连接地址无效。')
    try:
        parsed = urlsplit(api_url)
        port = parsed.port
    except ValueError:
        raise ValueError('原有记忆连接地址无效。') from None
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or port is None or not 1 <= port <= 65535):
        raise ValueError('原有记忆只使用已配置的本机 Hindsight 连接。')
    host = '[' + parsed.hostname + ']' if parsed.hostname == '::1' else parsed.hostname
    canonical_url = 'http://' + host + ':' + str(port)
    if (not isinstance(native_bank_id, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', native_bank_id)
            or '..' in native_bank_id):
        raise ValueError('原有记忆库编号无效。')
    identity = {'api_url': canonical_url, 'native_bank_id': native_bank_id}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    return dict(kind=KIND, **identity, source_fingerprint=fingerprint)


def validate_native_source(source):
    """Require the exact private source and verify its canonical fingerprint."""
    if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS):
        raise ValueError('原有记忆来源记录不完整。')
    expected = make_native_source(source['api_url'], source['native_bank_id'])
    if source != expected:
        raise ValueError('原有记忆来源身份无法核验。')
    return expected


def initialize_schema(db):
    """Participate in the caller's transaction without committing or inspecting data."""
    initialize_profiles(db)
    db.execute('''CREATE TABLE IF NOT EXISTS native_memory_bindings (
        profile_id TEXT PRIMARY KEY REFERENCES memory_profiles(id),
        kind TEXT NOT NULL CHECK(kind='hermes_hindsight_readonly'),
        api_url TEXT NOT NULL,
        native_bank_id TEXT NOT NULL CHECK(length(native_bank_id) BETWEEN 1 AND 128),
        source_fingerprint TEXT NOT NULL UNIQUE CHECK(length(source_fingerprint)=64),
        created_at TEXT NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS native_memory_binding_insert_readonly
        BEFORE INSERT ON native_memory_bindings
        WHEN NOT EXISTS(SELECT 1 FROM memory_profiles WHERE id=NEW.profile_id
            AND mode='read_only' AND program='hindsight' AND connection_id='local/hindsight')
        BEGIN SELECT RAISE(ABORT,'native_memory_read_only'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS native_memory_binding_update_immutable
        BEFORE UPDATE ON native_memory_bindings
        BEGIN SELECT RAISE(ABORT,'native_memory_binding_immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS native_memory_binding_delete_immutable
        BEFORE DELETE ON native_memory_bindings
        BEGIN SELECT RAISE(ABORT,'native_memory_binding_immutable'); END''')
    # Ordinary MemoryProfiles connections do not turn on foreign_keys. Guard the
    # parent too, so a read-only link cannot be orphaned or become a write target.
    db.execute('''CREATE TRIGGER IF NOT EXISTS native_memory_profile_identity_readonly
        BEFORE UPDATE OF id,bank_id,program,connection_id,mode ON memory_profiles
        WHEN EXISTS(SELECT 1 FROM native_memory_bindings WHERE profile_id=OLD.id)
            AND (NEW.id<>OLD.id OR NEW.bank_id<>OLD.bank_id OR NEW.program<>OLD.program
                OR NEW.connection_id<>OLD.connection_id OR NEW.mode<>'read_only')
        BEGIN SELECT RAISE(ABORT,'native_memory_read_only'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS native_memory_profile_delete_immutable
        BEFORE DELETE ON memory_profiles
        WHEN EXISTS(SELECT 1 FROM native_memory_bindings WHERE profile_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'native_memory_binding_immutable'); END''')


class NativeMemoryBindings(MemoryProfiles):
    """Private binding operations beside the existing local memory profiles."""
    def __init__(self, path):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._check_path()
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._open(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            initialize_schema(db)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _open(self, *, readonly=False, create=False):
        with super()._open(readonly=readonly, create=create) as db:
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    @staticmethod
    def _binding_in(db, profile_id):
        row = db.execute('SELECT ' + ','.join(BINDING_COLUMNS) +
            ' FROM native_memory_bindings WHERE profile_id=?', (profile_id,)).fetchone()
        if row is None:
            return None
        binding = dict(row)
        validate_memory_profile_id(binding['profile_id'])
        validate_native_source({field: binding[field] for field in SOURCE_FIELDS})
        profile = MemoryProfiles.get_in(db, profile_id)
        if profile['mode'] != 'read_only':
            raise ValueError('原有记忆关联必须保持只读取。')
        if not isinstance(binding['created_at'], str) or not binding['created_at']:
            raise ValueError('原有记忆关联记录不完整。')
        return binding

    @staticmethod
    def get_in(db, profile_id):
        """Read through the caller's transaction; never initialize a schema.

        Older databases without this optional table contain owned profiles.
        An existing but malformed table cannot establish that provenance.
        """
        validate_memory_profile_id(profile_id, allow_none=True)
        row = db.execute("SELECT type FROM sqlite_master WHERE name='native_memory_bindings'").fetchone()
        if row is None:
            return None
        expected = [(name, 'TEXT', 0 if index == 0 else 1, None, 1 if index == 0 else 0)
                    for index, name in enumerate(BINDING_COLUMNS)]
        if row[0] != 'table' or [tuple(item[1:6]) for item in
                db.execute('PRAGMA table_info(native_memory_bindings)')] != expected:
            raise ValueError('原有记忆关联表结构无法核验。')
        return None if profile_id == 'none' else NativeMemoryBindings._binding_in(db, profile_id)

    @classmethod
    def _find_in(cls, db, source):
        row = db.execute('SELECT profile_id FROM native_memory_bindings '
                         'WHERE source_fingerprint=?', (source['source_fingerprint'],)).fetchone()
        if row is None:
            return None
        binding = cls._binding_in(db, row['profile_id'])
        if {field: binding[field] for field in SOURCE_FIELDS} != source:
            raise ValueError('原有记忆关联来源无法核验。')
        return MemoryProfiles.get_in(db, row['profile_id'])

    def get(self, profile_id):
        """Read a private binding; explicit none or an owned profile has no link."""
        validate_memory_profile_id(profile_id, allow_none=True)
        if profile_id == 'none':
            return None
        with self.lock, self._open(readonly=True) as db:
            return self._binding_in(db, profile_id)

    def find_linked(self, source):
        source = validate_native_source(source)
        with self.lock, self._open(readonly=True) as db:
            return self._find_in(db, source)

    def attach(self, source, *, name='Hermes 原有记忆', description='', habits=''):
        """Create/link atomically; retry returns the existing, current profile.

        The source comes from server-side native configuration, never browser
        form fields. Its existence and read-only driver verification are the
        caller's responsibility. No physical bank is created by this method.
        """
        source = validate_native_source(source)
        fields = validate_memory_profile_fields(name, description, habits, mode='read_only')
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = self._find_in(db, source)
            if existing is not None:
                return existing, False
            profile = MemoryProfiles._insert_in(db, fields)
            db.execute('''INSERT INTO native_memory_bindings
                (profile_id,kind,api_url,native_bank_id,source_fingerprint,created_at)
                VALUES(?,?,?,?,?,?)''', (profile['id'],) +
                tuple(source[field] for field in SOURCE_FIELDS) + (profile['created_at'],))
            self._binding_in(db, profile['id'])
            return profile, True
