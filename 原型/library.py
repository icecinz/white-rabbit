"""One read-only list over shared chats, native directories and text history.

No migration is needed: keys identify the existing source, and only list rows are
merged. A linked source is represented by its shared-chat row, including later
native search matches and an update hint; independent branches remain available.
Explicit folder keys may retain the original history row after import/adoption.
"""
from contextlib import closing
import datetime as dt
from functools import lru_cache
import hashlib
import json
import sqlite3
from urllib.parse import quote

from chat_store import BusyError, ChatStore
from agent_registry import has as registered_agent
from native_catalogue import CATALOGUE_TABLES, MAX_CHATS, chat_id as native_chat_id, _title as native_title
from history_view import (_agent, _query, _integer, _excerpt, _INTERNAL_SESSION_SQL, history_order, history_position,
                          _is_approval_context, display_message, NATIVE_ATTACHMENT_SOURCE_FIELDS)


def _object(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _stamp(value):
    try:
        instant = dt.datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=dt.timezone.utc)
        return instant.astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, TypeError, OverflowError):
        return ''


def _agents(backend, provenance, object_reader=_object):
    # An absent/unknown source is never silently labeled Codex.
    return {value for value in (backend, object_reader(provenance).get('agent'))
            if isinstance(value, str) and value and len(value) <= 256}


def _library_keys(values):
    """Canonical existing-library identities supplied by trusted callers only."""
    if values is None:
        return None
    # Bound before deduplication. History SIDs are opaque source identities;
    # splitting at most twice preserves any colons belonging to the SID.
    if type(values) not in (list, tuple) or len(values) > 1000:
        raise ValueError('Invalid library keys')
    for value in values:
        if type(value) is not str:
            raise ValueError('Invalid library key')
        parts = value.split(':', 2)
        if len(parts) == 3 and parts[0] == 'history':
            LibraryView._source_pair((parts[1], parts[2]))
            continue
        if not (len(parts) == 2 and parts[0] == 'chat' or
                len(parts) == 3 and parts[0] == 'native' and parts[1] in CATALOGUE_TABLES):
            raise ValueError('Invalid library key')
        native_chat_id(parts[-1])
    return tuple(sorted(set(values)))


class _CollectAgents:
    def __init__(self, object_reader=_object):
        self.values = set()
        self.object_reader = object_reader

    def step(self, backend, provenance):
        self.values.update(_agents(backend, provenance, self.object_reader))

    def finalize(self):
        return json.dumps(sorted(self.values))


def _native_signature(value):
    try:
        items = json.loads(value) if isinstance(value, str) else value
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return "invalid_native_descriptors"
        projected = [{key: item.get(key) for key in NATIVE_ATTACHMENT_SOURCE_FIELDS} for item in items]
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        return "invalid_native_descriptors"


class _CollectNativeDescriptors:
    def __init__(self):
        self.values = []

    def step(self, *values):
        self.values.append(dict(zip(NATIVE_ATTACHMENT_SOURCE_FIELDS, values)))

    def finalize(self):
        self.values.sort(key=lambda item: item['attachment_ordinal'])
        return _native_signature(self.values)


class LibraryView:
    def __init__(self, store, history, *, excluded_source_getter=None, excluded_sources_getter=None,
                 enabled_native_agents_getter=None, native_bindings_getter=None):
        if excluded_source_getter is not None and not callable(excluded_source_getter):
            raise ValueError('Excluded source getter must be callable')
        if excluded_sources_getter is not None and not callable(excluded_sources_getter):
            raise ValueError('Excluded sources getter must be callable')
        if enabled_native_agents_getter is not None and not callable(enabled_native_agents_getter):
            raise ValueError('Enabled native agents getter must be callable')
        if native_bindings_getter is not None and not callable(native_bindings_getter):
            raise ValueError('Native bindings getter must be callable')
        self.store, self.history = store, history
        self._excluded_source_getter = excluded_source_getter
        self._excluded_sources_getter = excluded_sources_getter
        self._enabled_native_agents_getter = enabled_native_agents_getter
        self._native_bindings_getter = native_bindings_getter

    def _excluded_source(self):
        # A trusted application callback reads binding metadata only. Never
        # discover an identity from a title, serviceName or indexed content.
        value = self._excluded_source_getter() if self._excluded_source_getter is not None else None
        if value is None:
            return None
        return self._source_pair(value)

    @staticmethod
    def _source_pair(value):
        if type(value) is not tuple or len(value) != 2:
            raise ValueError('Invalid excluded native source')
        agent, session_id = value
        _agent(agent, allow_all=False)
        if (type(session_id) is not str or not 1 <= len(session_id) <= 256
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in session_id)):
            raise ValueError('Invalid excluded native source')
        session_id.encode('utf-8')
        return agent, session_id

    def _excluded_sources(self):
        # The application supplies explicit bindings, not titles or contents.
        # Bound before deduplication so duplicate input cannot evade the limit.
        values = self._excluded_sources_getter() if self._excluded_sources_getter is not None else ()
        if type(values) not in (list, tuple) or len(values) > 16384:
            raise ValueError('Invalid excluded native sources')
        return tuple(sorted({self._source_pair(value) for value in values}))

    def _history_state(self):
        try:
            metadata = self.history.index_path.stat()
            return [metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns]
        except OSError:
            return ['unavailable']

    def _enabled_native_agents(self):
        values = self._enabled_native_agents_getter() if self._enabled_native_agents_getter is not None else ()
        if (type(values) not in (list, tuple) or len(values) > len(CATALOGUE_TABLES)
                or any(type(agent) is not str or agent not in CATALOGUE_TABLES for agent in values)
                or len(set(values)) != len(values)):
            raise ValueError('Invalid enabled native agents')
        return tuple(sorted(values))

    def _native_bindings(self):
        # None preserves the old title-only/excluded-pair contract. A supplied
        # empty list is an authoritative observation with no published links.
        if self._native_bindings_getter is None:
            return None
        values = self._native_bindings_getter()
        if type(values) is not list or len(values) > MAX_CHATS * 128:
            raise ValueError('Invalid native bindings')
        rows, sources, adopted, sessions, activity = {}, {}, {}, {}, {}
        for value in values:
            if (type(value) is not dict or not {'agent', 'chat_id', 'session_id', 'adopted'} <= set(value)
                    or set(value) - {'agent', 'chat_id', 'session_id', 'adopted', 'activity_at'}
                    or type(value['agent']) is not str or value['agent'] not in ('codex', 'hermes', 'pi', 'claude', 'dsh')
                    or type(value['adopted']) is not bool):
                raise ValueError('Invalid native binding')
            identity = native_chat_id(value['chat_id'])
            agent, session = self._source_pair((value['agent'], value['session_id']))
            chat, source = (agent, identity), (agent, session)
            row = dict(agent=agent, chat_id=identity, session_id=session, adopted=value['adopted'])
            if 'activity_at' in value:
                timestamp = value['activity_at']
                if timestamp is not None and (type(timestamp) is not str or not _stamp(timestamp)):
                    raise ValueError('Invalid native activity timestamp')
                timestamp = _stamp(timestamp) if timestamp is not None else None
                if chat in activity and activity[chat] != timestamp:
                    raise ValueError('Ambiguous native activity')
                activity[chat] = timestamp
                row['activity_at'] = timestamp
            if ((chat in adopted and adopted[chat] != row['adopted'])
                    or (source in sources and sources[source] != chat)):
                raise ValueError('Ambiguous native binding')
            sessions.setdefault(chat, set()).add(session)
            if len(sessions[chat]) > (128 if agent == 'hermes' else 1):
                raise ValueError('Native binding exceeds its source limit')
            rows[(agent, identity, session)] = row
            sources[source], adopted[chat] = chat, row['adopted']
        return tuple(rows[key] for key in sorted(rows))

    @staticmethod
    def _native_catalogue(connection, enabled_agents, *, schema='main'):
        # Use the caller's existing read snapshot. Never instantiate a catalogue:
        # its constructor initializes tables and a disabled entry still belongs
        # in this directory. Only these fixed application metadata tables apply.
        if schema not in ('main', 'common'):
            raise ValueError('Invalid catalogue schema')
        tables = {row[0] for row in connection.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
        overlay = 'native_chat_display_names' in tables
        selects, parameters, rows = [], [], []
        for agent, table in CATALOGUE_TABLES.items():
            if table not in tables:
                continue
            title = 'COALESCE(n.title,c.title)' if overlay else 'c.title'
            select = f'SELECT c.id,? AS agent,{title} AS title,c.created_at FROM {schema}.{table} c'
            params = [agent]
            if overlay:
                select += (f' LEFT JOIN {schema}.native_chat_display_names n'
                           ' ON n.agent=? AND n.chat_id=c.id')
                params.append(agent)
            stored = connection.execute(select, params).fetchmany(MAX_CHATS + 1)
            if len(stored) > MAX_CHATS:
                raise ValueError('Native catalogue exceeds its limit')
            for row in stored:
                timestamp = _stamp(row['created_at'])
                if type(row['created_at']) is not str or not timestamp:
                    raise ValueError('Invalid native catalogue timestamp')
                rows.append({'id': native_chat_id(row['id']), 'agent': agent,
                             'title': native_title(row['title']), 'created_at': row['created_at'],
                             'timestamp': timestamp, 'available': agent in enabled_agents})
            selects.append(select)
            parameters.extend(params)
        rows.sort(key=lambda row: (row['agent'], row['id']))
        select = ' UNION ALL '.join(selects) or 'SELECT NULL AS id,NULL AS agent,NULL AS title,NULL AS created_at WHERE 0'
        return rows, select, parameters

    def revision(self, store_revision=None, *, keys=None):
        # Callbacks may acquire native-manager/store locks. Never call them
        # while holding ChatStore.lock or a read transaction over its database.
        keys = _library_keys(keys)
        excluded_source, excluded_sources = self._excluded_source(), self._excluded_sources()
        enabled_agents = self._enabled_native_agents()
        native_bindings = self._native_bindings()
        with self.store.lock, self.store._open(readonly=True) as connection:
            connection.execute('BEGIN')
            native_rows, _select, _parameters = self._native_catalogue(connection, enabled_agents)
            if store_revision is None:
                store_revision = str(connection.execute("SELECT value FROM store_meta WHERE key='revision'").fetchone()[0])
            return self._revision(store_revision, excluded_source, excluded_sources, native_rows,
                                  native_bindings=native_bindings, keys=keys)

    def _revision(self, store_revision, excluded_source, excluded_sources=(), native_rows=(), *,
                  history_state=None, native_bindings=None, keys=None):
        parts = [store_revision, self._history_state() if history_state is None else history_state]
        if excluded_source is not None:
            parts.append(excluded_source)
        if excluded_sources:
            parts.append(['excluded_sources', excluded_sources])
        if native_rows:
            parts.append(['native_catalogue', native_rows])
        if native_bindings is not None:
            parts.append(['native_bindings', native_bindings])
        if keys is not None:
            parts.append(['keys', keys])
        value = json.dumps(parts, separators=(',', ':'))
        return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]

    @staticmethod
    def _register(connection):
        # The connection belongs to one list snapshot. Reuse parsed metadata
        # within that request only; large values bypass the bounded caches.
        object_cache = lru_cache(maxsize=4096)(_object)
        native_cache = lru_cache(maxsize=512)(_native_signature)
        def object_reader(value):
            return object_cache(value) if isinstance(value, str) and len(value) <= 4096 else _object(value)
        def native_reader(value):
            return native_cache(value) if isinstance(value, str) and len(value) <= 8192 else _native_signature(value)
        connection.create_function('contains_fold', 2,
            lambda text, word: int(str(word).casefold() in str(text or '').casefold()))
        connection.create_function('is_approval_context', 1, lambda text: int(_is_approval_context(text)))
        connection.create_function('stamp', 1, _stamp)
        connection.create_function('import_stamp', 1, lambda value: _stamp(object_reader(value).get('timestamp')))
        connection.create_function('source_agent', 1, lambda value: object_reader(value).get('agent', ''))
        connection.create_function('source_session', 1, lambda value: object_reader(value).get('session_id', ''))
        connection.create_function('source_message', 1, lambda value: object_reader(value).get('message_id', ''))
        connection.create_function('has_agent', 3, lambda backend, source, agent: int(agent in _agents(backend, source, object_reader)))
        connection.create_aggregate('collect_agents', 2, lambda: _CollectAgents(object_reader))
        connection.create_function('native_signature', 1, native_reader)
        connection.create_aggregate('collect_native_descriptors', len(NATIVE_ATTACHMENT_SOURCE_FIELDS), _CollectNativeDescriptors)

    def list(self, query='', agent='all', offset=0, limit=50, include_internal=False, *, keys=None):
        agent, words = _agent(agent), _query(query)
        offset, limit = (_integer(offset, 'offset', 0, 1000000000),
                         _integer(limit, 'limit', 1, 100))
        if not isinstance(include_internal, bool):
            raise ValueError('include_internal must be boolean')
        keys = _library_keys(keys)
        excluded_source, excluded_sources = self._excluded_source(), self._excluded_sources()
        enabled_agents = self._enabled_native_agents()
        native_bindings = self._native_bindings()
        history_state = self._history_state()
        with self.store.lock:
            try:
                connection = self.history._open()
                has_history, notice = True, None
            except (OSError, sqlite3.Error, ValueError):
                # ATTACH below uses an explicit read-only file URI even when
                # the native history is absent. Enable URI parsing on this
                # fallback too; some SQLite builds do not enable it globally.
                connection = sqlite3.connect(':memory:', uri=True)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                has_history, notice = False, '历史索引尚未就绪；现有对话仍然可以使用。'
            with closing(connection):
                uri = 'file:' + quote(str(self.store.path), safe='/') + '?mode=ro'
                connection.execute('ATTACH DATABASE ? AS common', (uri,))
                self._register(connection)
                native_rows, native_sql, native_params = self._native_catalogue(connection, enabled_agents, schema='common')
                native_by_key = {'native:' + row['agent'] + ':' + row['id']: row for row in native_rows}
                store_revision = str(connection.execute("SELECT value FROM common.store_meta WHERE key='revision'").fetchone()[0])
                revision = self._revision(store_revision, excluded_source, excluded_sources, native_rows,
                                          history_state=history_state, native_bindings=native_bindings, keys=keys)
                base_revision = self._revision(store_revision, excluded_source, excluded_sources, native_rows,
                    history_state=history_state, native_bindings=native_bindings) if keys is not None else revision
                if keys is not None:
                    selected_keys = frozenset(keys)
                    connection.create_function('is_selected_library_key', 1,
                        lambda key: int(key in selected_keys))
                items, total = [], 0
                if keys != ():
                    owned_native = frozenset(excluded_sources)
                    published_bindings = {}
                    native_activity = {}
                    for row in native_bindings or ():
                        if 'native:' + row['agent'] + ':' + row['chat_id'] in native_by_key:
                            binding = published_bindings.setdefault((row['agent'], row['chat_id']),
                                dict(agent=row['agent'], adopted=row['adopted'], session_ids=[]))
                            binding['session_ids'].append(row['session_id'])
                            if row.get('activity_at') is not None:
                                native_activity[(row['agent'], row['chat_id'])] = row['activity_at']
                    connection.create_function('native_activity_stamp', 2,
                        lambda agent, identity: native_activity.get((agent, identity), ''))
                    if native_bindings is not None:
                        # A reserved binding without a catalogue publication must
                        # not erase the only navigable history row after a crash.
                        owned_native = frozenset(pair for pair in excluded_sources if pair[0] not in ('codex', 'hermes', 'pi', 'claude')) | frozenset(
                            (row['agent'], session) for row in published_bindings.values() for session in row['session_ids'])
                    indexed_bindings, indexed_sources = {}, set()
                    if has_history:
                        for identity, binding in published_bindings.items():
                            for session in binding['session_ids']:
                                source = connection.execute('SELECT kind FROM sessions WHERE agent=? AND session_id=?',
                                    (binding['agent'], session)).fetchone()
                                if source is not None:
                                    count = connection.execute('SELECT COUNT(*) FROM app_history_messages WHERE agent=? AND session_id=?',
                                        (binding['agent'], session)).fetchone()[0]
                                    indexed_bindings.setdefault(identity, []).append(dict(agent=binding['agent'],
                                        session_id=session, kind=source['kind'], message_count=count))
                                    indexed_sources.add((*identity, session))
                    connection.create_function('is_native_index_source', 3,
                        lambda agent, identity, session: int((agent, identity, session) in indexed_sources))
                    if owned_native:
                        connection.create_function('is_owned_native_source', 2,
                            lambda agent, session: int((agent, session) in owned_native))
                    common_columns = {row['name'] for row in connection.execute('PRAGMA common.table_info(messages)')}
                    common_native = 'cm.native_attachments' if 'native_attachments' in common_columns else "'[]'"
                    common_conditions, common_params = [], []
                    if keys is not None:
                        common_conditions.append("is_selected_library_key('chat:'||c.id)")
                    if agent != 'all':
                        common_conditions.append('EXISTS (SELECT 1 FROM common.messages m WHERE m.chat_id=c.id AND has_agent(m.backend,m.provenance,?))')
                        common_params.append(agent)
                    for word in words:
                        condition = '(contains_fold(c.title,?) OR EXISTS (SELECT 1 FROM common.messages m WHERE m.chat_id=c.id AND contains_fold(m.content,?))'
                        common_params.extend((word, word))
                        if has_history:
                            condition += ' OR EXISTS (SELECT 1 FROM app_history_messages nm WHERE nm.agent=source_agent(c.source) AND nm.session_id=source_session(c.source) AND contains_fold(nm.content,?))'
                            condition += ' OR EXISTS (SELECT 1 FROM sessions ns WHERE ns.agent=source_agent(c.source) AND ns.session_id=source_session(c.source) AND contains_fold(ns.title,?))'
                            common_params.extend((word, word))
                        common_conditions.append(condition + ')')
                    common_where = ' WHERE ' + ' AND '.join(common_conditions) if common_conditions else ''
                    source_time = ("(SELECT MAX(stamp(s.updated_at)) FROM sessions s WHERE s.agent=source_agent(c.source) "
                                   "AND s.session_id=source_session(c.source))") if has_history else "''"
                    native_time = ("(SELECT MAX(stamp(nm.timestamp)) FROM app_history_messages nm WHERE nm.agent=source_agent(c.source) "
                                   "AND nm.session_id=source_session(c.source))") if has_history else "''"
                    common_select = '''SELECT 'chat' AS kind,'chat:'||c.id AS key,c.id AS id,
                        NULL AS agent,NULL AS session_id,c.title,
                        MAX(COALESCE(NULLIF((SELECT MAX(stamp(m.created_at)) FROM common.messages m
                            WHERE m.chat_id=c.id AND m.status!='imported'),''),
                            NULLIF((SELECT MAX(import_stamp(m.provenance)) FROM common.messages m
                            WHERE m.chat_id=c.id AND m.status='imported'),''),NULLIF(''' + source_time + ''',''),
                            CASE WHEN NOT EXISTS(SELECT 1 FROM common.messages m WHERE m.chat_id=c.id)
                                AND c.source IS NULL THEN stamp(c.created_at) ELSE '' END),
                            COALESCE(NULLIF(''' + native_time + ''',''),NULLIF(''' + source_time + ''',''),'')) AS timestamp,
                        COALESCE((SELECT collect_agents(m.backend,m.provenance) FROM common.messages m WHERE m.chat_id=c.id),'[]') AS source_agents,
                        (SELECT COUNT(*) FROM common.messages m WHERE m.chat_id=c.id) AS message_count,
                        EXISTS(SELECT 1 FROM common.runs r WHERE r.chat_id=c.id AND r.status IN ('queued','running')) AS running,
                        NULL AS history_kind,c.source AS linked_source FROM common.chats c''' + common_where
                    history_params, history_select = [], ''
                    if has_history:
                        if keys is not None:
                            # Explicit history references already bypass ownership.
                            # Apply that exact scope before any search or projection.
                            conditions = ["is_selected_library_key('history:'||s.agent||':'||s.session_id)"]
                        else:
                            ownership = ['NOT EXISTS(SELECT 1 FROM common.chats c WHERE source_agent(c.source)=s.agent AND source_session(c.source)=s.session_id)',
                                "NOT EXISTS(SELECT 1 FROM common.messages cm WHERE cm.status='imported' AND source_agent(cm.provenance)=s.agent AND source_session(cm.provenance)=s.session_id)"]
                            if owned_native:
                                ownership.append('NOT is_owned_native_source(s.agent,s.session_id)')
                            conditions = [' AND '.join(ownership)]
                        if excluded_source is not None:
                            # This independent entry stays excluded even when
                            # include_internal reveals child/approval records.
                            conditions.append('NOT (s.agent=? AND s.session_id=?)')
                            history_params.extend(excluded_source)
                        if agent != 'all':
                            conditions.append('s.agent=?')
                            history_params.append(agent)
                        for word in words:
                            conditions.append('(contains_fold(s.title,?) OR EXISTS(SELECT 1 FROM app_history_messages m WHERE m.agent=s.agent AND m.session_id=s.session_id AND contains_fold(m.content,?)))')
                            history_params.extend((word, word))
                        if not include_internal:
                            conditions.append('NOT ' + _INTERNAL_SESSION_SQL)
                        history_select = ''' UNION ALL SELECT 'history', 'history:'||s.agent||':'||s.session_id,
                            NULL,s.agent,s.session_id,s.title,
                            COALESCE(NULLIF((SELECT MAX(stamp(m.timestamp)) FROM app_history_messages m WHERE m.agent=s.agent AND m.session_id=s.session_id),''),stamp(s.updated_at)),
                            NULL,(SELECT COUNT(*) FROM app_history_messages m WHERE m.agent=s.agent AND m.session_id=s.session_id),0,s.kind,NULL
                            FROM sessions s WHERE ''' + ' AND '.join(conditions)
                    native_conditions, native_filter_params = [], []
                    if keys is not None:
                        native_conditions.append("is_selected_library_key('native:'||n.agent||':'||n.id)")
                    if agent != 'all':
                        native_conditions.append('n.agent=?')
                        native_filter_params.append(agent)
                    for word in words:
                        condition = '(contains_fold(n.title,?)'
                        native_filter_params.append(word)
                        if has_history and indexed_bindings:
                            condition += (' OR EXISTS (SELECT 1 FROM app_history_messages m WHERE m.agent=n.agent'
                                          ' AND is_native_index_source(n.agent,n.id,m.session_id) AND contains_fold(m.content,?))')
                            native_filter_params.append(word)
                        native_conditions.append(condition + ')')
                    native_where = ' WHERE ' + ' AND '.join(native_conditions) if native_conditions else ''
                    native_select = ''' UNION ALL SELECT 'native','native:'||n.agent||':'||n.id,n.id,
                        n.agent,NULL,n.title,MAX(stamp(n.created_at),native_activity_stamp(n.agent,n.id)),NULL,NULL,NULL,NULL,NULL
                        FROM native_catalogue n''' + native_where
                    sql = 'WITH native_catalogue AS (' + native_sql + '), library AS (' + common_select + history_select + native_select + ') '
                    parameters = native_params + common_params + history_params + native_filter_params
                    membership = ''
                    if keys is not None:
                        membership = ' WHERE is_selected_library_key(key)'
                    total = connection.execute(sql + 'SELECT COUNT(*) FROM library' + membership, parameters).fetchone()[0]
                    rows = connection.execute(sql + 'SELECT * FROM library' + membership + ' ORDER BY timestamp DESC,key LIMIT ? OFFSET ?',
                        parameters + [limit, offset]).fetchall()
                    items = []
                    for row in rows:
                        item = dict(row)
                        history_kind = item.pop('history_kind')
                        linked_source = _object(item.pop('linked_source'))
                        if item['kind'] == 'native':
                            item.pop('session_id')
                            metadata = native_by_key[item['key']]
                            identity = (item['agent'], item['id'])
                            item.update(title=metadata['title'], created_at=metadata['created_at'], source_agents=[item['agent']],
                                        search_scope='title', timestamp_kind=('catalogued' if
                                            published_bindings.get(identity, {}).get('adopted') else 'created'),
                                        available=metadata['available'])
                            if native_activity.get(identity, '') > metadata['timestamp']:
                                item['timestamp_kind'] = 'confirmed_activity'
                            indexed = indexed_bindings.get(identity)
                            if indexed is not None:
                                sources = [{key: source[key] for key in ('agent', 'session_id')} for source in indexed]
                                item.update(search_scope='indexed_text',
                                    index_source=sources[0], index_sources=sources,
                                    indexed_message_count=sum(source['message_count'] for source in indexed))
                            if words and all(word.casefold() in item['title'].casefold() for word in words):
                                item['match'] = {'query': query.strip(), 'excerpt': item['title'], 'title_only': True}
                            elif words and indexed is not None:
                                match = self._match(connection, item, None, words, query.strip())
                                if match:
                                    item['indexed_match'] = match
                            items.append(item)
                            continue
                        item['running'] = bool(item['running'])
                        if item['kind'] == 'history':
                            source = connection.execute('SELECT * FROM sessions WHERE agent=? AND session_id=?',
                                (item['agent'], item['session_id'])).fetchone()
                            item['title'] = self.history._session(connection, source)['title']
                            item['source_agents'] = [item['agent']] if item['agent'] else []
                            item.pop('id')
                        else:
                            item['source_agents'] = json.loads(item['source_agents'])
                            item.pop('agent')
                            item.pop('session_id')
                            if registered_agent(linked_source.get('agent')) and isinstance(linked_source.get('session_id'), str):
                                source = {key: linked_source[key] for key in ('agent', 'session_id')}
                                count = connection.execute("SELECT COUNT(*) FROM common.messages WHERE chat_id=? AND status='imported' AND source_agent(provenance)=? AND source_session(provenance)=?",
                                    (item['id'], source['agent'], source['session_id'])).fetchone()[0]
                                native = connection.execute('SELECT * FROM sessions WHERE agent=? AND session_id=?',
                                    (source['agent'], source['session_id'])).fetchone() if has_history else None
                                indexed = connection.execute("SELECT COUNT(*) FROM app_history_messages WHERE agent=? AND session_id=? AND role IN ('user','assistant')",
                                    (source['agent'], source['session_id'])).fetchone()[0] if native is not None else None
                                selection = self.history.context_selection(connection, source['agent'], source['session_id']) if native is not None else None
                                selected_ids = selection['ids'] if selection is not None else None
                                if selected_ids is not None:
                                    indexed = len(selected_ids)
                                modified = False
                                if native is not None and indexed == count:
                                    if selected_ids is not None:
                                        copied_ids = [row[0] for row in connection.execute("SELECT source_message(provenance) FROM common.messages "
                                            "WHERE chat_id=? AND status='imported' AND source_agent(provenance)=? AND source_session(provenance)=? ORDER BY ordinal",
                                            (item['id'],source['agent'],source['session_id']))]
                                        modified = copied_ids != selected_ids
                                    selected_where = (' AND nm.message_id IN (' + ','.join('?' for _ in selected_ids) + ')'
                                        if selected_ids else ' AND 0') if selected_ids is not None else ''
                                    modified = bool(connection.execute("""SELECT EXISTS(SELECT 1 FROM app_history_messages nm
                                        WHERE nm.agent=? AND nm.session_id=? AND nm.role IN ('user','assistant')""" + selected_where + """
                                        AND NOT EXISTS(SELECT 1 FROM common.messages cm WHERE cm.chat_id=? AND cm.status='imported'
                                            AND source_agent(cm.provenance)=nm.agent AND source_session(cm.provenance)=nm.session_id
                                            AND source_message(cm.provenance)=nm.message_id AND cm.role=nm.role
                                            AND cm.content=nm.content AND cm.phase=COALESCE(nm.phase,'')
                                            AND native_signature(""" + common_native + """)=COALESCE((SELECT collect_native_descriptors(
                                                na.attachment_ordinal,na.content_index,na.field,na.token,na.fingerprint,
                                                na.kind,na.mime,na.size_bytes,na.state,na.reason,na.omitted_count)
                                                FROM app_history_attachments na WHERE na.agent=nm.agent AND na.session_id=nm.session_id
                                                AND na.message_id=nm.message_id),'[]')))""",
                                        (source['agent'], source['session_id'], *(selected_ids or ()), item['id'])).fetchone()[0]) or modified
                                item['source_update'] = {'source': source, 'indexed_messages': indexed,
                                    'imported_messages': count, 'has_changes': native is not None and (indexed != count or modified)}
                                if native is not None:
                                    item['source_update']['source_title'] = self.history._session(connection, native)['title']
                        if words:
                            match = None
                            update = item.get('source_update')
                            if update and has_history:
                                # Prefer the native location when a query term exists only
                                # there; opening an old copy would otherwise hide the hit.
                                body_words = [word for word in words if word.casefold() not in item['title'].casefold()]
                                native_words = [word for word in body_words if not connection.execute('SELECT EXISTS(SELECT 1 FROM common.messages WHERE chat_id=? AND contains_fold(content,?))',
                                    (item['id'], word)).fetchone()[0]]
                                if native_words:
                                    source = update['source']
                                    original = connection.execute('SELECT title,kind FROM sessions WHERE agent=? AND session_id=?',
                                        (source['agent'], source['session_id'])).fetchone()
                                    if original is not None:
                                        native_title = update.get('source_title', original['title'])
                                        native_item = dict(kind='history', title=native_title, **source)
                                        match = self._match(connection, native_item, original['kind'], native_words, query.strip())
                                        if match is None and all(word.casefold() in native_title.casefold() for word in native_words):
                                            match = {'index': 0, 'query': query.strip(), 'excerpt': native_title, 'title_only': True}
                                        if match:
                                            match.update(kind='history', **source)
                            match = match or self._match(connection, item, history_kind, words, query.strip())
                            if match:
                                item['match'] = match
                        items.append(item)
        # Release both the common read transaction and ChatStore.lock before
        # re-reading native metadata. This retains the existing change fence
        # without the Library -> native-manager lock order.
        if revision != self.revision(keys=keys):
            raise BusyError('历史刚刚更新了，请稍后刷新列表。')
        following = offset + len(items)
        result = {'items': items, 'total': total,
                  'next_offset': following if following < total else None, 'revision': revision}
        if keys is not None:
            result['base_revision'] = base_revision
        if notice:
            result['notice'] = notice
        return result

    @staticmethod
    def _match(connection, item, kind, words, query):
        """Return the first relevant message's exact position, not a page index."""
        title = item['title']
        # Terms already satisfied by the title need not occur again in a message.
        body_words = [word for word in words if word.casefold() not in title.casefold()]
        if not body_words:
            return None
        if item['kind'] == 'chat':
            # Source-image backfill can place a new physical row before an old
            # UUID without rewriting old ordinals. Use the same logical order
            # as ChatStore.get/context, inside this attached read snapshot.
            messages = ChatStore.messages_in(connection, item['id'], schema='common')
            for index, message in enumerate(messages):
                content = message.get('content') or ''
                if any(word.casefold() in content.casefold() for word in body_words):
                    return {'message_id': message['id'], 'index': index, 'query': query,
                            'excerpt': _excerpt(content, body_words)}
            return None
        elif item['kind'] == 'native':
            # A compression chain is one directory entry. Select its earliest
            # indexed hit, then calculate the position within that exact SID.
            params = [item['agent'], item['id']]
            sql = ('SELECT m.message_id,m.content,m.role,m.timestamp,m.ordinal,m.phase,m.session_id,s.kind'
                   ' FROM app_history_messages m JOIN sessions s ON s.agent=m.agent AND s.session_id=m.session_id'
                   ' WHERE m.agent=? AND is_native_index_source(m.agent,?,m.session_id)')
            order = ' ORDER BY ' + history_order(item['agent'], 'm.') + ',m.session_id'
        else:
            scope = 'm.agent=? AND m.session_id=?'
            params = [item['agent'], item['session_id']]
            sql = 'SELECT m.message_id,m.content,m.role,m.timestamp,m.ordinal,m.phase FROM app_history_messages m WHERE ' + scope
            order = ' ORDER BY ' + history_order(item['agent'], 'm.')
        # A search may match different turns. Pick the earliest matching turn;
        # full-list search still requires every term somewhere in the dialogue.
        sql += ' AND (' + ' OR '.join('contains_fold(m.content,?)' for word in body_words) + ')' + order + ' LIMIT 1'
        row = connection.execute(sql, params + body_words).fetchone()
        if row is None:
            return None
        native = item['kind'] == 'native'
        session_id = row['session_id'] if native else item['session_id']
        kind = row['kind'] if native else kind
        content = row['content']
        position = history_position(item['agent'], row)
        index = connection.execute('SELECT COUNT(*) FROM app_history_messages WHERE agent=? AND session_id=? AND ('
            + history_order(item['agent']) + ')<(' + ','.join('?' for _ in position) + ')',
            (item['agent'], session_id, *position)).fetchone()[0]
        visible = display_message(content, item['agent'], row['role'], kind, phase=row['phase']).get('display_content', content)
        if any(word.casefold() in visible.casefold() for word in body_words):
            content = visible
        result = {'message_id': row['message_id'], 'index': index, 'query': query,
                  'excerpt': _excerpt(content, body_words)}
        if native:
            result.update(agent=item['agent'], session_id=session_id)
        return result
