"""Typed, metadata-only references to existing indexed conversations.

The UUID names an organization document, never an Agent session. Navigation
keeps the exact Agent/SID pair; no native registration or message read occurs.
"""
import hashlib
import json
import re
import unicodedata
import uuid


_AGENTS = frozenset(('codex', 'pi', 'hermes', 'claude', 'dsh'))
_NAMESPACE = uuid.UUID('377c6ea6-66a7-4cc6-9f93-667f684016a7')


def reference_source(agent, session_id):
    """Validate exact index identities without trimming or UUID assumptions."""
    if (type(agent) is not str or agent not in _AGENTS
            or type(session_id) is not str or not 1 <= len(session_id) <= 256
            or any(char.isspace() or unicodedata.category(char) in ('Cc', 'Cf', 'Cs')
                   for char in session_id)):
        raise ValueError('Invalid indexed reference source')
    return {'agent': agent, 'session_id': session_id}


def reference_identity(workspace_id, agent, session_id):
    """Return the document UUID and stable identity revision, not a body hash."""
    if type(workspace_id) is not str or not re.fullmatch(r'[0-9a-f]{64}', workspace_id):
        raise ValueError('Invalid indexed reference workspace')
    source = reference_source(agent, session_id)
    name = json.dumps(['rabbit.history-reference', 1, workspace_id,
                       source['agent'], source['session_id']],
                      ensure_ascii=False, separators=(',', ':'))
    return str(uuid.uuid5(_NAMESPACE, name)), hashlib.sha256(name.encode('utf-8')).hexdigest()


class HistoryMarkdownReference:
    def __init__(self, history, workspace_id):
        reference_identity(workspace_id, 'codex', 'workspace-validation')
        self.history, self.workspace_id = history, workspace_id

    def get(self, agent, session_id):
        source = reference_source(agent, session_id)
        metadata = self.history.indexed_session_metadata(agent, session_id)
        if (type(metadata) is not dict or set(metadata) != {'agent', 'session_id', 'title'}
                or {key: metadata[key] for key in source} != source
                or type(metadata['title']) is not str or len(metadata['title']) > 4096):
            raise ValueError('Indexed reference metadata changed')
        metadata['title'].encode('utf-8')
        identity, _revision = reference_identity(self.workspace_id, agent, session_id)
        return dict(id=identity, kind='history_reference', **source,
                    title=metadata['title'] or '未命名对话')

    def render(self, agent, session_id):
        # Lazy import keeps this identity codec independent of the Markdown
        # parser and lets HistoryView reuse only its strict source validation.
        from .markdown_chat_document import render_history_reference
        value = self.get(agent, session_id)
        return render_history_reference({key: value[key] for key in ('agent', 'session_id', 'title')},
                                        self.workspace_id)
