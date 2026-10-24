"""Read-only bridge from published native identities to indexed text.

This module owns no Agent connection or copy of a native conversation. Catalogue
and exact source bindings are read around one HistoryView transaction; only the
selected identity participates in the content revision.
"""
from library import LibraryView
from native_catalogue import CATALOGUE_TABLES, chat_id as native_chat_id


class NativeMarkdownSource:
    def __init__(self, store, history, library):
        self.store, self.history, self.library = store, history, library

    def _metadata(self):
        # Match Library's lock order: manager metadata first, ChatStore second.
        # Do not instantiate NativeChatCatalogue: its constructor creates DDL.
        bindings = self.library._native_bindings() or ()
        with self.store.lock, self.store._open(readonly=True) as connection:
            connection.execute('BEGIN')
            rows, _select, _parameters = LibraryView._native_catalogue(connection, ())
        sources = {}
        for binding in bindings:
            sources.setdefault((binding['agent'], binding['chat_id']), []).append(binding['session_id'])
        return [dict(agent=row['agent'], id=row['id'], title=row['title'],
                     session_ids=sorted(sources[(row['agent'], row['id'])]))
                for row in rows if (row['agent'], row['id']) in sources]

    def candidates(self):
        """Navigation metadata only: never read indexed message bodies here."""
        return [dict(id=row['id'], agent=row['agent'], title=row['title'],
                     source_count=len(row['session_ids'])) for row in self._metadata()]

    def _identity(self, agent, chat_id):
        if type(agent) is not str or agent not in CATALOGUE_TABLES:
            raise ValueError('请选择已接入的原生 Agent。')
        native_chat_id(chat_id)
        for row in self._metadata():
            if (row['agent'], row['id']) == (agent, chat_id):
                return row
        raise KeyError('这段原生对话没有可核对的已登记来源。')

    def snapshot(self, agent, chat_id):
        before = self._identity(agent, chat_id)
        try:
            indexed = self.history.indexed_text_snapshot(agent, before['session_ids'])
        except KeyError:
            raise KeyError('至少一段原生来源尚未索引，没有创建不完整的整理文件。') from None
        if before != self._identity(agent, chat_id):
            raise ValueError('原生对话名称或来源刚刚变化，请重新预览；没有创建文件。')
        return dict(indexed, chat={'id': before['id'], 'title': before['title']})
