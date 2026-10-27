"""Durable Markdown organization with immutable first-publication receipts.

The host supplies a verified render result, never browser prose. Publication
receipts keep their original path; the separate read-only scanner observes
moves and edits by stable chat ID. Neither path copies content back to a chat.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

from .markdown_chat_directory import MarkdownChatDirectory, MarkdownDirectoryError, _folder_parts
from .markdown_chat_document import parse, document_identity, reference_identity, MarkdownChatDocumentError
from .project_publish_files import ProjectPublishError, _parts
from .markdown_sync_files import MarkdownMoveError
from .project_publish_store import (
    ProjectPublishStore, ProjectPublishStoreError, MESSAGES, MAX_PUBLICATIONS, _uuid,
)

_SHA = re.compile(r'[0-9a-f]{64}\Z')
_REVISION = re.compile(r'(?:0|[1-9][0-9]{0,18})\Z')
_SOURCE = {'kind', 'workspace_id', 'chat_id', 'revision', 'sha256', 'size_bytes'}
_AGENTS = {'codex', 'pi', 'hermes', 'claude', 'dsh'}
_CODES = set(MESSAGES) | {
    'invalid_document', 'document_changed', 'workspace_changed',
    'binding_exists', 'directory_unavailable', 'schema_changed',
}
_LIVE_BINDING = "client_id IS NOT NULL AND state IN ('publishing','verified')"
_SOURCE_PREDICATE = ("json_extract(artifact,'$.kind')=? AND "
                     "COALESCE(json_extract(artifact,'$.agent'),'')=? AND chat_id=?")
_OLD_INDEX_SQL = ('CREATE UNIQUE INDEX markdown_chat_one_binding '
                  'ON project_publications(chat_id) WHERE ' + _LIVE_BINDING)
_INDEX_SQL = ("CREATE UNIQUE INDEX markdown_chat_source_binding ON project_publications("
              "json_extract(artifact,'$.kind'),COALESCE(json_extract(artifact,'$.agent'),''),chat_id) "
              'WHERE ' + _LIVE_BINDING)
MAX_FILE_BYTES = 512 * 1024
_MESSAGES = dict(MESSAGES, **{
    'invalid_document': '整理文件的身份或格式无法核对，没有创建文件。',
    'document_changed': '整理全文与已核版本不一致，没有继续创建文件。',
    'workspace_changed': '工作区已变化，请重新打开整理入口。',
    'binding_exists': '这段聊天已有整理记录，请从文件夹查看；没有再建一份副本。',
    'directory_unavailable': '对话文件夹暂时无法完整核对，原聊天和文件保留。',
    'schema_changed': '整理记录的结构无法核对，原记录保留，没有自动修补。',
})


class MarkdownChatRegistryError(ValueError):
    def __init__(self, code):
        self.code = code if code in _CODES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


@contextmanager
def _errors():
    try:
        yield
    except MarkdownChatRegistryError:
        raise
    except ProjectPublishStoreError as error:
        raise MarkdownChatRegistryError(error.code) from None
    except MarkdownDirectoryError:
        raise MarkdownChatRegistryError('directory_unavailable') from None
    except MarkdownChatDocumentError:
        raise MarkdownChatRegistryError('invalid_document') from None
    except (OSError, sqlite3.Error):
        raise MarkdownChatRegistryError('io_failed') from None


def _source(value, workspace_id=None):
    native = type(value) is dict and value.get('kind') == 'native_index'
    reference = type(value) is dict and value.get('kind') == 'history_reference'
    indexed = native or reference
    fields = _SOURCE | ({'agent', 'session_id'} if reference else {'agent'} if native else set())
    if (type(value) is not dict or set(value) != fields
            or value.get('kind') not in ('markdown_chat', 'native_index', 'history_reference')
            or type(value['workspace_id']) is not str or not _SHA.fullmatch(value['workspace_id'])
            or workspace_id is not None and value['workspace_id'] != workspace_id
            or type(value['revision']) is not str
            or not (_SHA if indexed else _REVISION).fullmatch(value['revision'])
            or not indexed and int(value['revision']) > 2**63 - 1
            or type(value['sha256']) is not str or not _SHA.fullmatch(value['sha256'])
            or type(value['size_bytes']) is not int or not 0 <= value['size_bytes'] <= MAX_FILE_BYTES
            or indexed and (type(value['agent']) is not str or value['agent'] not in _AGENTS)):
        raise MarkdownChatRegistryError('invalid_document')
    try:
        _uuid(value['chat_id'])
        if reference:
            identity, revision = reference_identity(value['workspace_id'], value['agent'], value['session_id'])
            if (value['chat_id'], value['revision']) != (identity, revision):
                raise ValueError('Reference identity mismatch')
    except (ProjectPublishStoreError, ValueError, TypeError):
        raise MarkdownChatRegistryError('invalid_document') from None
    return dict(value)


def source_identity(source):
    """The app UUID is stable within its source namespace, not across Agents."""
    source = _source(source)
    if source['kind'] in ('native_index', 'history_reference'):
        return (source['kind'], source['agent'], source['chat_id'])
    return source['chat_id']


def _source_parameters(source):
    return source['kind'], source.get('agent', ''), source['chat_id']


def _install_index(db, existing_scope):
    expected = {'markdown_chat_one_binding': _OLD_INDEX_SQL,
                'markdown_chat_source_binding': _INDEX_SQL}
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' "
                      "AND tbl_name='project_publications' AND sql IS NOT NULL").fetchall()
    names = {row['name'] for row in rows}
    if (not names <= set(expected) or len(names) > 1 or existing_scope and not names):
        raise MarkdownChatRegistryError('schema_changed')
    for row in rows:
        if ' '.join(row['sql'].split()) != ' '.join(expected[row['name']].split()):
            raise MarkdownChatRegistryError('schema_changed')
    if 'markdown_chat_one_binding' in names:
        db.execute('DROP INDEX markdown_chat_one_binding')
    if 'markdown_chat_source_binding' not in names:
        db.execute(_INDEX_SQL)


class MarkdownChatRegistry:
    def __init__(self, data_root, documents_root, *, workspace_id, clock=time.time):
        if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id):
            raise MarkdownChatRegistryError('invalid_arguments')
        self.workspace_id = workspace_id
        self.publisher = None
        self.sync = None
        try:
            with _errors():
                self.directory = MarkdownChatDirectory(documents_root, workspace_id=workspace_id)
                self.publisher = ProjectPublishStore(data_root, documents_root, clock=clock)
                self.lock = self.publisher.lock
                scope = json.dumps(
                    [workspace_id, str(self.directory.root), self.directory._lineage],
                    separators=(',', ':'),
                )
                with self.publisher._db(write=True) as db:
                    db.execute('''CREATE TABLE IF NOT EXISTS markdown_chat_scope (
                        id INTEGER PRIMARY KEY CHECK(id=1), scope TEXT NOT NULL)''')
                    row = db.execute('SELECT scope FROM markdown_chat_scope WHERE id=1').fetchone()
                    if row is not None and row[0] != scope:
                        raise MarkdownChatRegistryError('scope_changed')
                    if row is None:
                        db.execute('INSERT INTO markdown_chat_scope VALUES(1,?)', (scope,))
                    # The new organizer owns this database. Generated-artifact
                    # publications are not migrated into it.
                    rows = db.execute('SELECT * FROM project_publications').fetchall()
                    if len(rows) > MAX_PUBLICATIONS:
                        raise MarkdownChatRegistryError('capacity')
                    for item in rows:
                        self._descriptor(dict(item))
                    _install_index(db, existing_scope=row is not None)
                from .markdown_chat_sync import MarkdownChatSync
                self.sync = MarkdownChatSync(self, clock=clock)
        except BaseException:
            if self.sync is not None:
                self.sync.close()
            if self.publisher is not None:
                self.publisher.close()
            raise

    def close(self):
        with self.lock, _errors():
            self.sync.close()
            self.publisher.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _source(self, value):
        return _source(value, self.workspace_id)

    def _descriptor(self, row):
        try:
            source = self._source(json.loads(row['artifact']))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise MarkdownChatRegistryError('invalid_document') from None
        if (row['chat_id'] != source['chat_id'] or row['sha256'] != source['sha256']
                or row['size_bytes'] != source['size_bytes']):
            raise MarkdownChatRegistryError('document_changed')
        return source

    def _publication(self, value):
        result = dict(value)
        result['source'] = self._source(result.pop('artifact'))
        return result

    def _receipt(self, value):
        if not value['received']:
            return {'received': False}
        return dict(value, publication=self._publication(value['publication']))

    def prepare(self, document, relative_path):
        """Accept only the host's complete deterministic render result."""
        with self.lock, _errors():
            if type(document) is not dict or set(document) != {'data', 'sha256', 'size_bytes', 'metadata'}:
                raise MarkdownChatRegistryError('invalid_document')
            raw = document['data']
            if type(raw) is not bytes:
                raise MarkdownChatRegistryError('invalid_document')
            if len(raw) > MAX_FILE_BYTES:
                raise MarkdownChatRegistryError('capacity')
            parsed = parse(raw, workspace_id=self.workspace_id)
            if (parsed['metadata'] != document['metadata'] or document['sha256'] != parsed['sha256']
                    or type(document['size_bytes']) is not int or document['size_bytes'] != len(raw)
                    or not parsed['body_matches_header']):
                raise MarkdownChatRegistryError('document_changed')
            if type(relative_path) is not str or Path(relative_path).suffix.casefold() != '.md':
                raise MarkdownChatRegistryError('invalid_arguments')
            metadata = parsed['metadata']
            chat = metadata['rabbit_id']
            indexed = metadata['kind'] in ('native_index', 'history_reference')
            descriptor = dict(
                kind=metadata['kind'] if indexed else 'markdown_chat', workspace_id=self.workspace_id,
                chat_id=chat, revision=metadata['revision'], sha256=parsed['sha256'], size_bytes=len(raw),
            )
            if indexed:
                descriptor['agent'] = metadata['source']['agent']
            if metadata['kind'] == 'history_reference':
                descriptor['session_id'] = metadata['source']['session_id']
            source = self._source(descriptor)
            with self.publisher._db() as db:
                if db.execute('SELECT 1 FROM project_publications WHERE ' + _SOURCE_PREDICATE +
                              ' AND ' + _LIVE_BINDING, _source_parameters(source)).fetchone():
                    raise MarkdownChatRegistryError('binding_exists')
            try:
                text = raw.decode('utf-8')
            except UnicodeError:
                raise MarkdownChatRegistryError('invalid_document') from None
            value = self.publisher._prepare_text(source, chat, text, relative_path)
            return dict(value, publication=self._publication(value['publication']))

    def get(self, publication_id):
        with self.lock, _errors():
            return self._publication(self.publisher.get(publication_id))

    def preview(self, publication_id):
        with self.lock, _errors():
            value = self.publisher.preview(publication_id)
            return dict(value, publication=self._publication(value['publication']))

    def list(self, chat_id=None, *, include_prepared=False, limit=100):
        with self.lock, _errors():
            if chat_id is None:
                return [self._publication(value) for value in self.publisher.list(
                    include_prepared=include_prepared, limit=limit)]
            if type(limit) is not int or not 1 <= limit <= MAX_PUBLICATIONS or type(include_prepared) is not bool:
                raise MarkdownChatRegistryError('invalid_arguments')
            if (type(chat_id) is tuple and len(chat_id) == 3
                    and chat_id[0] in ('native_index', 'history_reference')):
                if type(chat_id[1]) is not str or chat_id[1] not in _AGENTS:
                    raise MarkdownChatRegistryError('invalid_arguments')
                parameters = (chat_id[0], chat_id[1], _uuid(chat_id[2]))
            elif type(chat_id) is str:
                parameters = ('markdown_chat', '', _uuid(chat_id))
            else:
                raise MarkdownChatRegistryError('invalid_arguments')
            where = _SOURCE_PREDICATE + ('' if include_prepared else ' AND client_id IS NOT NULL')
            with self.publisher._db() as db:
                rows = [dict(row) for row in db.execute('SELECT * FROM project_publications WHERE ' + where +
                    ' ORDER BY created_at DESC,id LIMIT ?', (*parameters, limit))]
            return [self._publication(self.publisher._public(row)) for row in rows]

    def find_receipt(self, client_id):
        with self.lock, _errors():
            return self._receipt(self.publisher.find_receipt(client_id))

    def confirm(self, publication_id, client_id):
        # The legacy publisher replays a client before parsing a new ID.
        # Organization requires the exact pair, without changing that API.
        with self.lock, _errors():
            publication_id, client_id = _uuid(publication_id), _uuid(client_id)
            previous = self.publisher.find_receipt(client_id)
            if previous['received']:
                if previous['publication']['id'] != publication_id:
                    raise MarkdownChatRegistryError('operation_conflict')
                return self._receipt(previous)
            row = self.publisher._row(publication_id)
            source = self._descriptor(row)
            with self.publisher._db() as db:
                accepted = db.execute('SELECT id FROM project_publications WHERE ' + _SOURCE_PREDICATE +
                    ' AND ' + _LIVE_BINDING, _source_parameters(source)).fetchone()
            if accepted is not None:
                raise MarkdownChatRegistryError('binding_exists')
            try:
                value = self.publisher.confirm(publication_id, client_id)
                # Another registry can win this client before the base enters
                # its transaction. Do not accept that other publication ID.
                if value['publication']['id'] != publication_id:
                    raise MarkdownChatRegistryError('operation_conflict')
                if value['state'] == 'verified':
                    self.sync.register_binding(publication_id)
                return self._receipt(value)
            except ProjectPublishStoreError as error:
                # The partial UNIQUE index arbitrates other registry instances.
                # Inspect the committed result, never retry publication.
                if error.code != 'io_failed':
                    raise
                previous = self.publisher.find_receipt(client_id)
                if previous['received']:
                    if previous['publication']['id'] != publication_id:
                        raise MarkdownChatRegistryError('operation_conflict')
                    if previous['state'] == 'verified':
                        self.sync.register_binding(publication_id)
                    return self._receipt(previous)
                with self.publisher._db() as db:
                    accepted = db.execute('SELECT 1 FROM project_publications WHERE ' + _SOURCE_PREDICATE +
                        ' AND ' + _LIVE_BINDING, _source_parameters(source)).fetchone()
                if accepted is not None:
                    raise MarkdownChatRegistryError('binding_exists')
                raise

    def _base_bindings(self):
        """Original immutable publication anchors; never consult sync heads."""
        with self.lock, _errors():
            with self.publisher._db() as db:
                rows = [dict(row) for row in db.execute('''SELECT * FROM project_publications
                    WHERE state='verified' AND client_id IS NOT NULL ORDER BY chat_id,artifact''')]
            if len(rows) > MAX_PUBLICATIONS:
                raise MarkdownChatRegistryError('capacity')
            return [dict(publication_id=row['id'], source=self._descriptor(row),
                         target_path=row['target_path'], accepted_at=row['updated_at']) for row in rows]

    def bindings(self):
        """Latest accepted baseline; current paths still come only from scan."""
        with self.lock, _errors():
            return self.sync.overlay_bindings(self._base_bindings())

    def _locate_current_file(self, binding_id, expected_head_id):
        bindings = self.bindings()
        binding = next((item for item in bindings if item['publication_id'] == binding_id), None)
        if binding is None:
            raise MarkdownChatRegistryError('not_found')
        if binding['head_id'] != expected_head_id:
            raise MarkdownChatRegistryError('document_changed')
        references = {source_identity(item['source']): {
            'sha256': item['source']['sha256'], 'revision': item['source']['revision'],
        } for item in bindings}
        scan = self.directory.scan(references, current_revisions={
            identity: reference['revision'] for identity, reference in references.items()})
        if not scan['complete']:
            raise MarkdownChatRegistryError('directory_unavailable')
        source = binding['source']
        selected = next((row for row in scan['chats']
            if (row.get('kind', 'markdown_chat'), row.get('agent', ''), row['rabbit_id']) ==
               (source['kind'], source.get('agent', ''), source['chat_id'])), None)
        if (selected is None or selected['status'] not in ('current', 'external_modified')
                or len(selected['paths']) != 1 or 'file' not in selected):
            raise MarkdownChatRegistryError('document_changed')
        return binding, selected

    def current_file(self, binding_id, expected_head_id):
        """Read one current disk document as untrusted reference material.

        The accepted source only supplies navigation identity and a baseline;
        the returned content/hash belong to this disk observation, including
        explicit external edits. Nothing is imported or marked accepted here.
        """
        with self.lock, _errors():
            binding_id, expected_head_id = _uuid(binding_id), _uuid(expected_head_id)

            def inspect(path):
                try:
                    return self.publisher.files.inspect(path)
                except ProjectPublishError as error:
                    code = error.code if error.code in ('capacity', 'scope_changed', 'not_found', 'closed') else 'document_changed'
                    raise MarkdownChatRegistryError(code) from None

            binding, before = self._locate_current_file(binding_id, expected_head_id)
            path = before['paths'][0]
            current = inspect(path)
            if not current['exists'] or current['sha256'] != before['file']['sha256']:
                raise MarkdownChatRegistryError('document_changed')
            raw = current['text'].encode('utf-8')
            parsed = parse(raw, workspace_id=self.workspace_id)
            if document_identity(parsed['metadata']) != source_identity(binding['source']):
                raise MarkdownChatRegistryError('document_changed')
            after_binding, after = self._locate_current_file(binding_id, expected_head_id)
            if (after_binding != binding or after['paths'] != [path]
                    or after['file']['sha256'] != current['sha256']):
                raise MarkdownChatRegistryError('document_changed')
            last = inspect(path)
            if (not last['exists'] or any(last[key] != current[key]
                    for key in ('scope', 'identity', 'sha256', 'size_bytes'))
                    or self.sync.binding(binding_id) != binding):
                raise MarkdownChatRegistryError('document_changed')
            return dict(binding_id=binding_id, head_id=expected_head_id, source=dict(binding['source']),
                path=path, name=Path(path).name, content=current['text'], sha256=current['sha256'],
                size_bytes=current['size_bytes'], observation={
                    'file_status': after['status'], 'source_revision_checked': False})

    def move_file(self, binding_id, expected_head_id, source_path, source_sha256, target_folder):
        """Move one registered current disk file; no content or receipt is rewritten.

        This explicit action has no immutable move receipt. Unknown completion
        requires read-only observation, never automatic retry or rollback.
        """
        moved = False
        try:
            with self.lock:
                binding_id, expected_head_id = _uuid(binding_id), _uuid(expected_head_id)
                if type(source_sha256) is not str or _SHA.fullmatch(source_sha256) is None:
                    raise MarkdownMoveError('invalid_arguments')
                parts = _parts(source_path)
                if type(target_folder) is not str:
                    raise MarkdownMoveError('invalid_arguments')
                if target_folder:
                    _folder_parts(target_folder)
                target_path = '/'.join((target_folder, parts[-1])) if target_folder else parts[-1]
                _parts(target_path)
                if target_path == source_path:
                    raise MarkdownMoveError('invalid_arguments')
                self.sync._check()
                if not self.sync.supported:
                    raise MarkdownMoveError('unsupported_platform' if self.sync._support_error == 'unsupported_platform'
                                            else 'move_unsupported')
                with self.sync._ownership() as owned:
                    if not owned:
                        raise MarkdownMoveError('operation_conflict')
                    with self.publisher._db() as db:
                        if db.execute("SELECT 1 FROM markdown_sync_operations WHERE binding_id=? AND state='publishing'",
                                      (binding_id,)).fetchone():
                            raise MarkdownMoveError('operation_conflict')
                    current = self.current_file(binding_id, expected_head_id)
                    if current['path'] != source_path or current['sha256'] != source_sha256:
                        raise MarkdownMoveError('document_changed')
                    source = self.sync.files.inspect(source_path)
                    target = self.sync.files.inspect(target_path)
                    if (not source['exists'] or source['sha256'] != source_sha256
                            or source['size_bytes'] != current['size_bytes']):
                        raise MarkdownMoveError('document_changed')
                    if target['exists']:
                        raise MarkdownMoveError('target_exists')
                    binding, selected = self._locate_current_file(binding_id, expected_head_id)
                    if selected['paths'] != [source_path] or selected['file']['sha256'] != source_sha256:
                        raise MarkdownMoveError('document_changed')
                    result = self.sync.files.move_file(source_path, target_path, source['scope'],
                        source['identity'], source_sha256, source['size_bytes'], target['scope'])
                    moved = True
                    after_binding, after = self._locate_current_file(binding_id, expected_head_id)
                    if (after_binding != binding or after['paths'] != [target_path]
                            or after['file']['sha256'] != source_sha256):
                        raise MarkdownMoveError('move_unconfirmed', result_unknown=True)
                    result = dict(binding_id=binding_id, head_id=expected_head_id, **result)
                return result
        except Exception as error:
            if moved or getattr(error, 'result_unknown', False) is True:
                raise MarkdownMoveError('move_unconfirmed', result_unknown=True) from None
            if isinstance(error, MarkdownMoveError):
                raise
            code = getattr(error, 'code', 'io_failed')
            if isinstance(error, MarkdownDirectoryError):
                code = 'invalid_arguments' if code == 'invalid_folder' else 'directory_unavailable'
            raise MarkdownMoveError(code) from None

    def scan(self, current_revisions):
        with self.lock, _errors():
            references = {source_identity(row['source']): {
                'sha256': row['source']['sha256'], 'revision': row['source']['revision'],
            } for row in self.bindings()}
            return self.directory.scan(references, current_revisions=current_revisions)

    def recover_accepted(self):
        """Observe only original accepted stages; absence is never permission."""
        with self.lock, _errors():
            with self.publisher._db() as db:
                rows = [dict(row) for row in db.execute('''SELECT * FROM project_publications
                    WHERE state='publishing' AND client_id IS NOT NULL ORDER BY created_at,id''')]
            counts = dict(verified=0, conflict=0, blocked=0, publishing=0)
            for row in rows:
                self._descriptor(row)
                if self.publisher._stage(row['id']) is None:
                    self.publisher._finish(row, 'blocked', 'stage_missing')
                else:
                    self.publisher._publish(row)
                if self.publisher._row(row['id'])['state'] == 'verified':
                    self.sync.register_binding(row['id'])
                counts[self.publisher._row(row['id'])['state']] += 1
            for state, count in self.sync.recover_accepted().items():
                counts[state] += count
            return counts
