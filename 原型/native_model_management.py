"""Native model-directory operations, reusing each Agent's business boundary.

GETs only observe already open connections. The explicit preparation POST may
open one exact existing chat through its original Service route; it never
creates an app chat, sends a prompt, or replaces another connection.
"""
from contextlib import contextmanager
import re
from urllib.parse import parse_qs

from native_catalogue import chat_id, _title
from native_http_common import SpecialistHTTPError
from native_maintenance import try_locks
from rabbit_runtime.model_observation import CodexModelObservation, ModelObservationError


CONNECTIONS_PATH = '/api/management/native-model-connections'
CATALOG_PATH = '/api/management/native-model-catalog'
SPECIALIST_CATALOG_PATH = '/api/management/specialist-model-catalog'
PREPARATION_PATH = '/api/management/native-model-preparation'
_AGENTS = (('codex', 'native_chats', 2), ('pi', 'pi_native_chats', 1),
           ('hermes', 'hermes_native_chats', 1))
_EPOCH = re.compile(r'[0-9a-f]{32}\Z')


def _error(kind):
    messages = {'busy': '原生连接正在处理操作，请稍后再更新模型目录。',
                'changed': '原生连接已经变化，请重新选择当前连接。',
                'unavailable': '原生模型目录暂不可读，请核对已打开的连接。'}
    return SpecialistHTTPError(409, 'native_model_catalog_' + kind, messages[kind])


def invalid():
    raise SpecialistHTTPError(400, 'native_model_catalog_invalid', '原生模型目录请求格式无效。')


def query_fields(raw):
    """Exactly one of each public selector; no defaults or extra authority."""
    if type(raw) is not str or len(raw) > 512:
        invalid()
    try:
        fields = parse_qs(raw, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=3)
    except (ValueError, UnicodeError):
        invalid()
    if set(fields) != {'agent', 'chat_id', 'epoch'} or any(len(items) != 1 for items in fields.values()):
        invalid()
    return validate_fields({name: items[0] for name, items in fields.items()})


def validate_fields(fields):
    if type(fields) is not dict or set(fields) != {'agent', 'chat_id', 'epoch'}:
        invalid()
    if type(fields['agent']) is not str or fields['agent'] not in {row[0] for row in _AGENTS}:
        invalid()
    try:
        identity = chat_id(fields['chat_id'])
    except SpecialistHTTPError:
        invalid()
    if type(fields['epoch']) is not str or not _EPOCH.fullmatch(fields['epoch']):
        invalid()
    return {'agent': fields['agent'], 'chat_id': identity, 'epoch': fields['epoch']}


def preparation_fields(fields):
    if type(fields) is not dict or set(fields) != {'agent', 'chat_id'}:
        invalid()
    if type(fields['agent']) is not str or fields['agent'] not in {row[0] for row in _AGENTS}:
        invalid()
    try:
        identity = chat_id(fields['chat_id'])
    except SpecialistHTTPError:
        invalid()
    return {'agent': fields['agent'], 'chat_id': identity}


def prepare(service, fields):
    """Explicitly open the selected real chat, then observe that exact epoch.

    The existing route owns adoption checks, pending requests, connection
    limits and native initialization. Normal initialization can load extensions
    and write session metadata. A failed observation leaves the connection in
    its actual state: no cleanup, eviction, alternate chat or automatic retry.
    """
    fields = preparation_fields(fields)
    agent, identity = fields['agent'], fields['chat_id']
    manager = _manager(service, agent)
    if (manager is None or agent == 'pi' and (
            getattr(manager, 'profile_intents', None) is None
            or not callable(getattr(manager.connection, 'model_capabilities', None)))):
        raise _error('unavailable')
    route_name, root = {
        'codex': ('native_chat_request', '/api/native-chats/'),
        'pi': ('pi_native_request', '/api/pi-chats/'),
        'hermes': ('hermes_native_request', '/api/hermes-chats/'),
    }[agent]
    try:
        opened = getattr(service, route_name)('POST', root + identity + '/open', body={})
        if type(opened) is not dict or _manager(service, agent) is not manager:
            raise _error('changed')
        connection = opened if agent == 'codex' else opened.get('connection')
        if (type(connection) is not dict or connection.get('state') != 'ready'
                or (agent != 'codex' and (opened.get('workspace_id') != service.workspace_id
                                         or opened.get('chat_id') != identity))):
            raise _error('unavailable')
        # refresh validates the nonce and rechecks the registered owner before
        # and after RPC. An intervening disconnect/reopen cannot be mistaken
        # for the connection that this operation opened.
        return refresh(service, dict(fields, epoch=opened.get('epoch')))
    except SpecialistHTTPError as error:
        # Existing native errors may say accepted:false about a *message*.
        # This operation instead concerns a connection and may already have
        # opened it; do not turn that flag into proof of no side effects.
        error.accepted = None
        error.result_unknown = True
        raise
    except Exception:
        raise SpecialistHTTPError(503, 'native_model_preparation_unconfirmed',
            '模型目录尚未准备好；连接可能已经打开，请核对当前状态。', result_unknown=True) from None


def _manager(service, agent):
    name = next(row[1] for row in _AGENTS if row[0] == agent)
    manager = getattr(service, name, None)
    if agent == 'hermes' and manager is not None and not manager.profiles_enabled:
        return None
    return manager


def _registry(manager):
    with try_locks(manager._lock) as acquired:
        if not acquired:
            raise _error('busy')
        if manager._closing or getattr(manager, '_closed', False):
            raise _error('unavailable')
        return list(manager._owners.items())


def _holder(agent, owner):
    return owner.holder if agent == 'codex' else owner


@contextmanager
def _action_scope(agent, owner):
    if agent == 'codex':
        with try_locks(owner.lock) as acquired:
            if not acquired:
                raise _error('busy')
            observation = CodexModelObservation(owner.holder)
            try:
                with observation.action():
                    yield observation
            except ModelObservationError as error:
                raise _error(error.reason) from None
    else:
        with try_locks(owner._action) as acquired:
            if not acquired:
                raise _error('busy')
            yield None


def _identity_locked(agent, holder):
    """Pi/Hermes RAM only; Codex state stays inside its runtime adapter."""
    if (holder._state != 'ready' or getattr(holder, '_closed', False)
            or holder._rpc is None or not _EPOCH.fullmatch(holder._epoch or '')
            or holder._reader is None or not holder._reader.is_alive()):
        return None
    if agent == 'pi':
        if not holder._profiles_enabled or type(holder._live_identity) is not dict:
            return None
        native = (holder._live_identity.get('session_id'), holder._live_identity.get('session_file'))
    else:
        if not holder._profiles_enabled or holder._runtime is None or holder._live_key is None:
            return None
        native = (holder._runtime, holder._live_key)
    return holder._epoch, holder._rpc, holder._reader, native


@contextmanager
def _registered_owner(manager, identity, owner):
    with try_locks(manager._lock) as acquired:
        if not acquired:
            raise _error('busy')
        if (manager._closing or getattr(manager, '_closed', False)
                or manager._owners.get(identity) is not owner):
            raise _error('changed')
        yield


def _observe(manager, agent, identity, owner, *, observation=None, expected=None, remember=None):
    if agent == 'codex':
        if observation is None:
            raise _error('unavailable')
        return observation.observe(registry=lambda: _registered_owner(manager, identity, owner),
            expected=expected, remember=(None if remember is None else
                lambda: manager._remember_catalogue(identity, remember)))
    holder = _holder(agent, owner)
    with try_locks(holder._cache) as acquired:
        if not acquired:
            raise _error('busy')
        with _registered_owner(manager, identity, owner):
            current = _identity_locked(agent, holder)
            if expected is not None and current != expected:
                raise _error('changed')
            if remember is not None:
                manager._remember_catalogue(identity, remember)
            return current


def _snapshot_epoch(agent, snapshot):
    return snapshot.epoch if agent == 'codex' else snapshot[0]


@contextmanager
def _registered_specialist(service, manager, holder):
    # A reader may run from the specialist's own management tool. Keep these
    # registration guards short; neither a Service lock nor a DB spans RPC.
    with try_locks(service._specialist_lock, manager._lock) as acquired:
        if not acquired:
            raise _error('busy')
        if (service.specialist is not holder or service.native_chats is not manager
                or service._memory_stop.is_set() or manager._closing
                or getattr(manager, '_closed', False)
                or holder.session_key != 'specialist/codex'):
            raise _error('changed')
        yield


def refresh_specialist(service):
    """Read the already-ready specialist, without opening any ordinary chat.

    Only complete model/list observations enter the existing candidate cache.
    A normal-chat observation published during this read wins instead. There
    is no inference, process launch, thread creation or settings update here.
    """
    holder, manager = service.specialist, service.native_chats
    if holder is None or manager is None:
        raise _error('unavailable')
    observation = CodexModelObservation(holder)
    try:
        with observation.action():
            registry = lambda: _registered_specialist(service, manager, holder)
            before = observation.observe(registry=registry)
            if before is None:
                raise _error('unavailable')
            previous = manager.catalogue_snapshot()
            try:
                catalogue = holder.models()
            except Exception:
                raise _error('unavailable') from None
            if type(catalogue) is not dict or type(catalogue.get('data')) is not list or catalogue.get('nextCursor') is not None:
                raise _error('unavailable')
            def remember():
                if not manager._remember_catalogue(None, catalogue, expected=previous):
                    raise _error('changed')
            observation.observe(registry=registry, expected=before, remember=remember)
    except ModelObservationError as error:
        raise _error(error.reason) from None
    return {'workspace_id': service.workspace_id, 'agent': 'codex',
            'source': 'specialist', 'native_catalog_ready': True}


def connections(service):
    rows = []
    for agent, _name, limit in _AGENTS:
        manager = _manager(service, agent)
        if manager is None:
            continue
        candidates = []
        for identity, owner in _registry(manager):
            with _action_scope(agent, owner) as observation:
                snapshot = _observe(manager, agent, identity, owner, observation=observation)
                if snapshot is not None:
                    candidates.append((identity, owner, snapshot))
        if not candidates:
            continue
        if len(candidates) > limit:
            raise _error('unavailable')
        # Titles belong to the durable app catalogue. Read it once, outside all
        # native/manager locks; a rename needs no second RAM title cache.
        try:
            titles = {chat_id(row['id']): _title(row['title']) for row in manager.catalogue.list()}
        except Exception:
            raise _error('unavailable') from None
        for identity, owner, snapshot in candidates:
            if identity not in titles:
                raise _error('unavailable')
            with _action_scope(agent, owner) as observation:
                _observe(manager, agent, identity, owner, observation=observation, expected=snapshot)
            rows.append({'agent': agent, 'chat_id': identity, 'title': titles[identity],
                         'epoch': _snapshot_epoch(agent, snapshot)})
    return {'workspace_id': service.workspace_id, 'connections': rows}


def refresh(service, fields):
    fields = validate_fields(fields)
    agent, identity, epoch = (fields[key] for key in ('agent', 'chat_id', 'epoch'))
    manager = _manager(service, agent)
    if manager is None:
        raise _error('unavailable')
    owner = dict(_registry(manager)).get(identity)
    if owner is None:
        raise _error('changed')
    with _action_scope(agent, owner) as observation:
        before = _observe(manager, agent, identity, owner, observation=observation)
        if before is None or _snapshot_epoch(agent, before) != epoch:
            raise _error('changed')
        holder = _holder(agent, owner)
        try:
            # Pi/Hermes normally remember at their original profiles endpoint.
            # This endpoint commits only after its stronger identity recheck.
            catalogue = holder.models() if agent == 'codex' else holder.models(remember=False, refresh_state=False)
            if type(catalogue) is not dict:
                raise ValueError('Incomplete model directory.')
        except Exception:
            raise _error('unavailable') from None
        _observe(manager, agent, identity, owner, observation=observation, expected=before, remember=catalogue)
    return dict(fields, workspace_id=service.workspace_id, native_catalog_ready=True)
