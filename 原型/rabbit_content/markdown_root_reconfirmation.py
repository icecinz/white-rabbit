"""Explicit, offline root re-confirmation for a legacy Markdown registry.

This is not an HTTP or startup recovery action. A trusted maintenance caller
must own installation/service locks, have a consistent private backup, obtain
the user's explicit confirmation of the frozen root/files, and pass its own
SQLite transaction. This module never opens an Agent or database, commits the
caller's transaction, replays a publication, or changes an old receipt/scope.
Only a new, immutable root-anchor evidence row may be inserted.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from ._directory_identity import DirectoryPin, FilePin, validate_anchor
from .markdown_chat_registry import (
    MAX_FILE_BYTES, MAX_PUBLICATIONS, MarkdownChatRegistryError,
    _SHA, _anchor_schema, _checked_anchor, _compact, _errors,
    _legacy_scope, _root_anchor, _source, _text_sha256,
)
from .project_publish_files import ProjectPublishError, _parts


_QUERIES = {
    'markdown_chat_scope': 'SELECT id,scope FROM markdown_chat_scope ORDER BY id',
    'project_publications': '''SELECT id,artifact,chat_id,project_name,project_root,
        target_path,scope,target_exists,length(CAST(target_text AS BLOB)) AS target_text_bytes,
        sha256,size_bytes,state,client_id,error_code,created_at,updated_at,expires_at
        FROM project_publications ORDER BY id''',
    'project_publication_payloads': '''SELECT publication_id,length(payload) AS payload_bytes
        FROM project_publication_payloads ORDER BY publication_id''',
    'project_publication_stages': 'SELECT * FROM project_publication_stages ORDER BY sequence',
    'markdown_sync_heads': 'SELECT publication_id,head_id FROM markdown_sync_heads ORDER BY publication_id',
    'markdown_sync_operations': '''SELECT id,binding_id,expected_head_id,previous_source,
        source,target_path,scope,old_identity,length(payload) AS payload_bytes,state,
        client_id,stage_identity,error_code,retained,created_at,updated_at,expires_at
        FROM markdown_sync_operations ORDER BY id''',
    'sqlite_sequence': 'SELECT name,seq FROM sqlite_sequence ORDER BY name',
}
_ANCHOR_OBJECTS = frozenset(('markdown_root_anchor', 'markdown_root_anchor_no_update',
                           'markdown_root_anchor_no_delete'))


def _rows(db, query):
    cursor = db.execute(query)
    columns = [item[0] for item in cursor.description]
    values = cursor.fetchmany(MAX_PUBLICATIONS + 1)
    if len(values) > MAX_PUBLICATIONS:
        raise MarkdownChatRegistryError('capacity')
    return [dict(zip(columns, row)) for row in values]


def _metadata(db):
    schema = [list(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')
        if row[1] not in _ANCHOR_OBJECTS]
    names = {row[1] for row in schema if row[0] == 'table'}
    if names != set(_QUERIES):
        raise MarkdownChatRegistryError('schema_changed')
    values = {name: _rows(db, query) for name, query in _QUERIES.items()}
    return {'schema': schema, 'tables': values}


def _verified_sources(metadata, workspace_id):
    tables = metadata['tables']
    publications = tables['project_publications']
    operations = tables['markdown_sync_operations']
    if any(row['state'] in ('prepared', 'publishing') for row in publications + operations):
        raise MarkdownChatRegistryError('operation_conflict')
    heads = {row['publication_id']: row['head_id'] for row in tables['markdown_sync_heads']}
    completed = {row['id']: row for row in operations if row['state'] == 'verified'
                 and row['client_id'] is not None}
    result = {}
    for row in publications:
        source = _source(json.loads(row['artifact']), workspace_id)
        if (row['chat_id'], row['sha256'], row['size_bytes']) != (
                source['chat_id'], source['sha256'], source['size_bytes']):
            raise MarkdownChatRegistryError('document_changed')
        if row['state'] != 'verified' or row['client_id'] is None:
            continue
        if row['id'] not in heads:
            raise MarkdownChatRegistryError('schema_changed')
        head = heads[row['id']]
        if head != row['id']:
            update = completed.get(head)
            if update is None or update['binding_id'] != row['id']:
                raise MarkdownChatRegistryError('schema_changed')
            current = _source(json.loads(update['source']), workspace_id)
            if (current['kind'], current.get('agent'), current['chat_id']) != (
                    source['kind'], source.get('agent'), source['chat_id']):
                raise MarkdownChatRegistryError('document_changed')
            source = current
        result[row['id']] = source
    if set(heads) != set(result):
        raise MarkdownChatRegistryError('schema_changed')
    return result


def _file_hash(pin):
    before = os.fstat(pin.fd)
    if not 0 <= before.st_size <= MAX_FILE_BYTES:
        raise MarkdownChatRegistryError('capacity')
    hasher, offset = hashlib.sha256(), 0
    while True:
        chunk = os.pread(pin.fd, min(65536, MAX_FILE_BYTES + 1 - offset), offset)
        if not chunk:
            break
        hasher.update(chunk)
        offset += len(chunk)
        if offset > MAX_FILE_BYTES:
            raise MarkdownChatRegistryError('capacity')
    after = os.fstat(pin.fd)
    state = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if state(before) != state(after) or offset != before.st_size:
        raise MarkdownChatRegistryError('document_changed')
    pin.check()
    return {'sha256': hasher.hexdigest(), 'size_bytes': offset}


@contextmanager
def _guard(db, documents_root, workspace_id, files):
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)
            or type(files) is not list or len(files) > MAX_PUBLICATIONS):
        raise MarkdownChatRegistryError('invalid_arguments')
    with _errors(), DirectoryPin(documents_root) as root, ExitStack() as stack:
        metadata = _metadata(db)
        rows = metadata['tables']['markdown_chat_scope']
        if len(rows) != 1 or rows[0]['id'] != 1:
            raise MarkdownChatRegistryError('scope_changed')
        legacy = rows[0]['scope']
        _legacy_scope(legacy, workspace_id, root.path)
        existing = _root_anchor(db)
        if existing is not None:
            _checked_anchor(existing, legacy, workspace_id, root.path, root.fd)
        sources = _verified_sources(metadata, workspace_id)
        observed, opened, seen = [], [], set()
        for item in files:
            if (type(item) is not dict or set(item) != {
                    'publication_id', 'path', 'sha256', 'size_bytes'}
                    or type(item['publication_id']) is not str
                    or item['publication_id'] in seen or item['publication_id'] not in sources):
                raise MarkdownChatRegistryError('invalid_arguments')
            source = sources[item['publication_id']]
            if ((item['sha256'], item['size_bytes']) != (source['sha256'], source['size_bytes'])
                    or type(item['size_bytes']) is not int):
                raise MarkdownChatRegistryError('document_changed')
            try:
                parts = _parts(item['path'])
            except ProjectPublishError:
                raise MarkdownChatRegistryError('invalid_arguments') from None
            pin = stack.enter_context(FilePin(str(Path(root.path).joinpath(*parts))))
            current = _file_hash(pin)
            if current != {'sha256': item['sha256'], 'size_bytes': item['size_bytes']}:
                raise MarkdownChatRegistryError('document_changed')
            seen.add(item['publication_id'])
            observed.append(dict(item))
            opened.append((pin, current))
        if seen != set(sources):
            raise MarkdownChatRegistryError('document_changed')
        root.check()
        state = {'workspace_id': workspace_id, 'root': root.path,
            'legacy_scope_sha256': _text_sha256(legacy),
            'metadata_sha256': _text_sha256(_compact(metadata)),
            'root_anchor': root.anchor, 'files': sorted(observed, key=lambda item: item['publication_id'])}
        yield state, legacy, root
        for pin, current in opened:
            if _file_hash(pin) != current:
                raise MarkdownChatRegistryError('document_changed')
        root.check()


def root_reconfirmation_snapshot(db, documents_root, *, workspace_id, files):
    """Build a no-write plan using caller's read transaction and exact files.

    Files are explicit reviewed paths, not a scan/guess. Their hashes must cover
    every verified binding at its current head. Bodies enter only these local
    hash operations; no text or payload column is returned or selected.
    """
    with _guard(db, documents_root, workspace_id, files) as (state, _legacy, _root):
        return state


def reconfirm_root(db, documents_root, *, workspace_id, files,
                   expected_legacy_scope_sha256, expected_metadata_sha256,
                   expected_root_anchor):
    """Insert new evidence only after explicit confirmation of a frozen plan.

    Caller owns offline maintenance, backup, and BEGIN IMMEDIATE/COMMIT. The
    savepoint makes failures roll back this function's evidence even if the
    caller retains its surrounding transaction. No old scope/receipt is edited.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or type(expected_legacy_scope_sha256) is not str
            or not _SHA.fullmatch(expected_legacy_scope_sha256)
            or type(expected_metadata_sha256) is not str
            or not _SHA.fullmatch(expected_metadata_sha256)):
        raise MarkdownChatRegistryError('invalid_arguments')
    with _errors():
        expected = validate_anchor(expected_root_anchor)
        if expected['type'] != 'directory':
            raise MarkdownChatRegistryError('invalid_arguments')
        db.execute('SAVEPOINT markdown_root_reconfirm')
        try:
            with _guard(db, documents_root, workspace_id, files) as (state, legacy, root):
                if (state['legacy_scope_sha256'] != expected_legacy_scope_sha256
                        or state['metadata_sha256'] != expected_metadata_sha256
                        or state['root_anchor'] != expected):
                    raise MarkdownChatRegistryError('scope_changed')
                row = _root_anchor(db)
                if row is None:
                    _anchor_schema(db, create=True)
                    db.execute('INSERT INTO markdown_root_anchor VALUES(1,?,?,?,?)',
                        (workspace_id, root.path, expected_legacy_scope_sha256, _compact(expected)))
                    result = 'registered'
                else:
                    _checked_anchor(row, legacy, workspace_id, root.path, root.fd)
                    result = 'already_registered'
                if _text_sha256(_compact(_metadata(db))) != expected_metadata_sha256:
                    raise MarkdownChatRegistryError('document_changed')
            db.execute('RELEASE SAVEPOINT markdown_root_reconfirm')
            return {'state': result, 'scope_unchanged': True, 'receipts_unchanged': True,
                    'root_anchor': dict(expected), 'committed': False}
        except BaseException:
            db.execute('ROLLBACK TO SAVEPOINT markdown_root_reconfirm')
            db.execute('RELEASE SAVEPOINT markdown_root_reconfirm')
            raise
