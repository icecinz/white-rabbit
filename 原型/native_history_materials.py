"""Bounded, read-only native locators to immutable private image materials.

The caller obtains descriptors only from the private history index. This module
is not an HTTP path/URL reader. No model or network request happens here. Missing
originals remain explicit descriptors; they are never successful empty images.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import quote

from rabbit_content.materials import (HASH_PATTERN, MAX_FILE_BYTES, MaterialCapacityError, image_parts)

MAX_SOURCE_READ_BYTES = 256 * 1024 * 1024
MAX_SOURCE_LINES = 2_000_000
DESCRIPTOR_FIELDS = ('attachment_ordinal', 'content_index', 'field', 'token', 'fingerprint',
                     'kind', 'mime', 'size_bytes', 'state', 'reason', 'omitted_count')


class NativeImageReadError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class NativeHistoryMaterials:
    def __init__(self, store, indexer, codex_root=None, hermes_db=None):
        self.store, self.indexer = store, indexer
        self.codex_root = Path(codex_root or Path.home() / '.codex').expanduser().absolute()
        self.hermes_db = Path(hermes_db or Path.home() / '.hermes' / 'state.db').expanduser().absolute()

    @staticmethod
    def _source_path(value):
        if (not isinstance(value, str) or not value or len(value) > 8192 or '\x00' in value):
            raise NativeImageReadError('source_outside')
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise NativeImageReadError('source_outside')
        return path

    def _allowed_path(self, value, agent):
        path = self._source_path(value)
        if agent == 'codex':
            roots = (self.codex_root / 'sessions', self.codex_root / 'archived_sessions')
            if path.suffix != '.jsonl' or not any(root in path.parents for root in roots):
                raise NativeImageReadError('source_outside')
        elif agent == 'hermes':
            if path != self.hermes_db:
                raise NativeImageReadError('source_outside')
        else:
            raise NativeImageReadError('descriptor_invalid')
        return path

    @staticmethod
    @contextmanager
    def _open_source(path):
        # Every directory and the file stay anchored to opened descriptors;
        # checking is_symlink() before opening alone would leave a swap race.
        descriptors = []
        try:
            current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for part in path.parts[1:-1]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                descriptors.append(current)
            file = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            descriptors.append(file)
            metadata = os.fstat(file)
            if not stat.S_ISREG(metadata.st_mode):
                raise NativeImageReadError('source_unavailable')
            yield file, metadata, current
        except FileNotFoundError:
            raise NativeImageReadError('source_missing') from None
        except OSError:
            raise NativeImageReadError('source_unavailable') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _locator(descriptor, agent, session_id, role):
        try:
            value = json.loads(descriptor['locator_json'])
        except (KeyError, TypeError, ValueError):
            raise NativeImageReadError('descriptor_invalid') from None
        if (not isinstance(value, dict)
                or value.get('kind') != ('codex_jsonl' if agent == 'codex' else 'hermes_sqlite')
                or not isinstance(value.get('record_sha256'), str)
                or not HASH_PATTERN.fullmatch(value['record_sha256'])
                or value.get('field') not in ('content', 'api_content')
                or type(value.get('content_index')) is not int
                or not 0 <= value['content_index'] < 4096
                or value['content_index'] != descriptor.get('content_index')
                or value['field'] != descriptor.get('field')
                or value.get('agent') != agent or value.get('session_id') != session_id
                or value.get('role') != role or value.get('message_id') != descriptor.get('message_id')
                or descriptor.get('agent') != agent or descriptor.get('session_id') != session_id):
            raise NativeImageReadError('descriptor_invalid')
        return value

    def _codex_content(self, path, locator, session_id, role):
        target = locator.get('source_line')
        if type(target) is not int or not 1 <= target <= MAX_SOURCE_LINES or locator['field'] != 'content':
            raise NativeImageReadError('descriptor_invalid')
        total, current_session = 0, None
        limit = self.indexer.MAX_SOURCE_LINE_BYTES
        with self._open_source(path) as (descriptor, _metadata, _parent):
            with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                for number in range(1, target + 1):
                    line = handle.readline(min(limit + 1, MAX_SOURCE_READ_BYTES - total + 1))
                    total += len(line)
                    if total > MAX_SOURCE_READ_BYTES:
                        raise NativeImageReadError('source_read_limit')
                    if not line:
                        raise NativeImageReadError('source_missing')
                    if len(line) > limit:
                        if number == target:
                            raise NativeImageReadError('source_read_limit')
                        while not line.endswith(b'\n'):
                            line = handle.readline(min(limit + 1, MAX_SOURCE_READ_BYTES - total + 1))
                            total += len(line)
                            if total > MAX_SOURCE_READ_BYTES:
                                raise NativeImageReadError('source_read_limit')
                            if not line:
                                break
                        continue
                    if number == target:
                        if hashlib.sha256(line).hexdigest() != locator['record_sha256']:
                            raise NativeImageReadError('source_changed')
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            raise NativeImageReadError('source_changed') from None
                        payload = record.get('payload') if isinstance(record, dict) else None
                        if (not isinstance(record, dict) or current_session != session_id or record.get('type') != 'response_item'
                                or not isinstance(payload, dict) or payload.get('type') != 'message'
                                or payload.get('role') != role):
                            raise NativeImageReadError('descriptor_invalid')
                        return payload.get('content')
                    if b'"session_meta"' in line:
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(record, dict) and record.get('type') == 'session_meta':
                            payload = record.get('payload')
                            if isinstance(payload, dict):
                                current_session = payload.get('id') or payload.get('session_id')
        raise NativeImageReadError('source_missing')

    @staticmethod
    def _copy_snapshot_file(source, destination, before, remaining):
        if not stat.S_ISREG(before.st_mode) or before.st_size > remaining:
            raise NativeImageReadError('source_read_limit')
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        copied = 0
        try:
            with os.fdopen(output, 'wb') as handle:
                while True:
                    chunk = os.read(source, min(1024 * 1024, remaining - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > remaining:
                        raise NativeImageReadError('source_read_limit')
                    handle.write(chunk)
            after = os.fstat(source)
            if (copied != before.st_size or
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise NativeImageReadError('source_changed')
            return copied
        except OSError:
            raise NativeImageReadError('source_unavailable') from None

    @contextmanager
    def _hermes_snapshot(self, path):
        """Query a private bounded DB/WAL copy, never reopen the native path.

        SQLite opens filenames internally. Using the original path after a
        no-follow check would let a directory swap redirect that second open.
        Both native files are therefore copied only from anchored descriptors;
        any transient native update is checked again and also protected by the
        indexed carrier SHA. The native SHM file is neither opened nor changed.
        """
        with self._open_source(path) as (database, metadata, parent):
            wal, wal_before = None, None
            try:
                try:
                    wal = os.open(path.name + '-wal', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=parent)
                    wal_before = os.fstat(wal)
                    if not stat.S_ISREG(wal_before.st_mode):
                        raise NativeImageReadError('source_unavailable')
                except FileNotFoundError:
                    wal = None
                except OSError:
                    raise NativeImageReadError('source_unavailable') from None
                with tempfile.TemporaryDirectory(prefix='rabbit-native-db-') as temporary:
                    snapshot = Path(temporary) / 'snapshot.sqlite'
                    copied = self._copy_snapshot_file(database, snapshot, metadata, MAX_SOURCE_READ_BYTES)
                    if wal is not None:
                        self._copy_snapshot_file(wal, str(snapshot) + '-wal', wal_before,
                                                 MAX_SOURCE_READ_BYTES - copied)
                    after = os.fstat(database)
                    if ((metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) !=
                            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                        raise NativeImageReadError('source_changed')
                    yield snapshot
            finally:
                if wal is not None:
                    os.close(wal)

    def _hermes_content(self, path, locator, session_id, role):
        row_id, field = locator.get('row_id'), locator['field']
        if (isinstance(row_id, bool) or not isinstance(row_id, (str, int))
                or not str(row_id) or len(str(row_id)) > 256):
            raise NativeImageReadError('descriptor_invalid')
        with self._hermes_snapshot(path) as snapshot:
            connection = None
            try:
                connection = sqlite3.connect('file:' + quote(str(snapshot), safe='/') + '?mode=ro',
                                             uri=True, timeout=10)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(messages)')}
                if not {'id', 'session_id', 'role', field}.issubset(columns):
                    raise NativeImageReadError('source_unavailable')
                # field comes from a fixed allowlist, never an SQL identifier
                # supplied by HTTP. Do not fetch an oversized carrier at all.
                row = connection.execute(
                    'SELECT id,session_id,role,length(CAST(' + field + ' AS BLOB)) size,'
                    'CASE WHEN length(CAST(' + field + ' AS BLOB))<=? THEN ' + field + ' END carrier '
                    'FROM messages WHERE id=? AND session_id=? AND role=?',
                    (self.indexer.MAX_SOURCE_LINE_BYTES, row_id, session_id, role)).fetchone()
                if row is None:
                    raise NativeImageReadError('source_missing')
                if row['size'] is not None and row['size'] > self.indexer.MAX_SOURCE_LINE_BYTES:
                    raise NativeImageReadError('source_read_limit')
                if not isinstance(row['carrier'], str):
                    raise NativeImageReadError('source_unavailable')
                if hashlib.sha256(row['carrier'].encode('utf-8')).hexdigest() != locator['record_sha256']:
                    raise NativeImageReadError('source_changed')
                return row['carrier']
            except sqlite3.Error:
                raise NativeImageReadError('source_unavailable') from None
            finally:
                if connection is not None:
                    connection.close()

    def materialize(self, agent, session_id, role, descriptor):
        """Return a path-free descriptor with either a real ID or a reason."""
        if not isinstance(descriptor, dict):
            raise ValueError('原生图片描述需要完整记录。')
        result = {key: descriptor.get(key) for key in DESCRIPTOR_FIELDS}
        result.update(attachment_id=None, material_state='unavailable', material_reason='native_unavailable')
        if descriptor.get('state') != 'embedded':
            return result
        if (agent not in ('codex', 'hermes') or role not in ('user', 'assistant')
                or not isinstance(session_id, str) or not session_id or len(session_id) > 256
                or not isinstance(descriptor.get('sha256'), str)
                or not HASH_PATTERN.fullmatch(descriptor['sha256'])
                or not isinstance(descriptor.get('token'), str)
                or not re.fullmatch(r'natimg_[0-9a-f]{64}', descriptor['token'])
                or not isinstance(descriptor.get('fingerprint'), str)
                or not HASH_PATTERN.fullmatch(descriptor['fingerprint'])
                or descriptor.get('kind') not in ('input_image', 'image_url', 'legacy_image_data_url')
                or descriptor.get('mime') not in ('image/png', 'image/jpeg')
                or type(descriptor.get('size_bytes')) is not int
                or not 0 < descriptor['size_bytes'] <= MAX_FILE_BYTES):
            result['material_reason'] = 'descriptor_invalid'
            return result
        expected_token = 'natimg_' + hashlib.sha256(json.dumps([
            descriptor.get('field'), descriptor.get('content_index'), descriptor['fingerprint']],
            separators=(',', ':')).encode()).hexdigest()
        if descriptor['token'] != expected_token:
            result['material_reason'] = 'descriptor_invalid'
            return result
        try:
            locator = self._locator(descriptor, agent, session_id, role)
            path = self._allowed_path(locator.get('source_path'), agent)
        except NativeImageReadError as error:
            result['material_reason'] = error.reason
            return result
        try:
            existing = self.store.find_by_sha256(descriptor['sha256'])
        except (ValueError, KeyError, OSError):
            result['material_reason'] = 'storage_unavailable'
            return result
        if existing is not None:
            if existing['mime'] != descriptor['mime'] or existing['size_bytes'] != descriptor['size_bytes']:
                result['material_reason'] = 'descriptor_invalid'
                return result
            result.update(attachment_id=existing['id'], material_state='ready', material_reason=None)
            return result
        try:
            content = (self._codex_content(path, locator, session_id, role) if agent == 'codex'
                       else self._hermes_content(path, locator, session_id, role))
            try:
                raw = self.indexer.extract_native_attachment_bytes(
                    content, locator['content_index'], descriptor, field=locator['field'])
            except (ValueError, TypeError, KeyError):
                raise NativeImageReadError('descriptor_invalid') from None
        except NativeImageReadError as error:
            result['material_reason'] = error.reason
            return result
        try:
            image_parts(raw)
        except ValueError:
            result['material_reason'] = 'invalid_image'
            return result
        try:
            material = self.store.import_bytes('历史图片.png' if descriptor['mime'] == 'image/png'
                                               else '历史图片.jpg', raw)
        except MaterialCapacityError:
            result['material_reason'] = 'storage_capacity'
            return result
        except (ValueError, KeyError, OSError):
            result['material_reason'] = 'storage_unavailable'
            return result
        result.update(attachment_id=material['id'], material_state='ready', material_reason=None)
        return result
