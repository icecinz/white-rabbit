"""Application names only; no native holder, RPC, body or configuration access."""
from native_catalogue import MAX_CHATS, NativeChatNameConflict, chat_id, _title
from native_http_common import SpecialistHTTPError
from native_maintenance import try_locks


PATH = '/api/management/native-chat-names'
_AGENTS = (('codex', 'native_chats'), ('pi', 'pi_native_chats'),
           ('hermes', 'hermes_native_chats'), ('claude', 'claude_native_chats'),
           ('dsh', 'dsh_native_chats'))
FIELDS = ('agent', 'chat_id', 'expected_title', 'title')


def _scope(service):
    return {'workspace_id': service.workspace_id, 'scope': 'native_chat_names',
            'inference_performed': False}


class _Failure(SpecialistHTTPError):
    def __init__(self, service, status, code, message, *, current_chat=None):
        super().__init__(status, code, message, accepted=False)
        self.scope, self.current_chat = _scope(service), current_chat

    def payload(self):
        result = dict(super().payload(), **self.scope)
        if self.current_chat is not None:
            result['current_chat'] = self.current_chat
        return result


def invalid(service):
    raise _Failure(service, 400, 'native_chat_name_invalid', '对话名称管理请求格式无效。')


def _available(service, manager):
    stopped = getattr(service, '_memory_stop', None)
    if stopped is not None and stopped.is_set():
        raise _Failure(service, 503, 'native_chat_name_unavailable', '本机对话入口正在收尾。')
    if manager is None:
        raise _Failure(service, 404, 'native_chat_name_disabled', '这台入口尚未启用此原生对话。')
    with try_locks(manager._lock) as acquired:
        if not acquired:
            raise _Failure(service, 409, 'native_chat_name_busy', '对话入口正在处理操作，请稍后再试。')
        if manager._closing or getattr(manager, '_closed', False):
            raise _Failure(service, 503, 'native_chat_name_unavailable', '本机对话入口正在收尾。')
    # Never carry the manager lock into SQLite or construct/read an owner.
    return manager


def names(service):
    managers = [(agent, _available(service, manager)) for agent, name in _AGENTS
                if (manager := getattr(service, name, None)) is not None]
    stopped = getattr(service, '_memory_stop', None)
    if stopped is not None and stopped.is_set():
        raise _Failure(service, 503, 'native_chat_name_unavailable', '本机对话入口正在收尾。')
    rows = []
    for _agent, manager in managers:
        chats = manager.catalogue.list()
        if len(chats) > MAX_CHATS:
            raise _Failure(service, 503, 'native_chat_name_unavailable', '本机对话目录需要核对。')
        rows.extend(chats)
    return dict(_scope(service), chats=rows, enabled_agents=[agent for agent, _manager in managers])


def rename(service, fields):
    if type(fields) is not dict or set(fields) != set(FIELDS):
        invalid(service)
    if type(fields['agent']) is not str or fields['agent'] not in dict(_AGENTS):
        invalid(service)
    try:
        identity = chat_id(fields['chat_id'])
        _title(fields['expected_title'])
        title = _title(fields['title'])
    except SpecialistHTTPError:
        invalid(service)
    manager = _available(service, getattr(service, dict(_AGENTS)[fields['agent']], None))
    try:
        result = manager.catalogue.rename(identity, fields['expected_title'], title)
    except NativeChatNameConflict as error:
        raise _Failure(service, error.status, error.code, error.message,
                       current_chat=error.current_chat) from None
    except SpecialistHTTPError as error:
        raise _Failure(service, error.status, error.code, error.message) from None
    return dict(_scope(service), **result)
