"""Small observations of Rabbit-owned slots, never discovery of outside work.

The timestamp dates this cache read, not an Agent event. Nothing here opens an
owner, reads a transcript, issues RPC, or turns this observation into admission.
"""
from datetime import datetime, timezone
import re

from native_catalogue import chat_id
import native_maintenance
from native_maintenance import try_locks


PATH = '/api/management/work-status'
_AGENTS = (('codex', 'native_chats', 2), ('pi', 'pi_native_chats', 1),
           ('hermes', 'hermes_native_chats', 1), ('claude', 'claude_native_chats', 1),
           ('dsh', 'dsh_native_chats', 1))
_FIELDS = {'complete', 'reason', 'status', 'connection_state', 'epoch',
           'native_session_id', 'pending_requests'}
_CODE = re.compile(r'[a-z][a-z_]{0,47}\Z')
_EPOCH = re.compile(r'[0-9a-f]{32}\Z')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')


class _Incomplete(Exception):
    pass


def _raw(value):
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value['complete']) is not bool
            or value['reason'] is not None and (type(value['reason']) is not str or not _CODE.fullmatch(value['reason']))
            or type(value['status']) is not str or value['status'] not in ('running', 'needs_user', 'idle', 'unknown')
            or type(value['connection_state']) is not str
            or value['connection_state'] not in ('not_open', 'opening', 'ready', 'failed', 'closed', 'unknown')
            or value['epoch'] is not None and (type(value['epoch']) is not str or not _EPOCH.fullmatch(value['epoch']))
            or value['native_session_id'] is not None and (type(value['native_session_id']) is not str
                or not _SESSION.fullmatch(value['native_session_id']))
            or value['pending_requests'] is not None and (type(value['pending_requests']) is not int
                or not 0 <= value['pending_requests'] <= 129)):
        raise _Incomplete('invalid_observation')
    if (not value['complete'] and value['status'] != 'unknown'
            or value['status'] == 'idle' and (value['connection_state'] != 'ready'
                or value['epoch'] is None or value['native_session_id'] is None
                or value['pending_requests'] != 0)
            or value['status'] == 'needs_user' and (value['pending_requests'] is None
                or value['pending_requests'] <= 0)):
        raise _Incomplete('invalid_observation')
    return dict(value)


def _capture(service, agent, attribute, limit):
    """Copy reserved slots under registry locks; no holder action spans them."""
    manager = getattr(service, attribute, None)
    entries = []
    if manager is not None:
        with try_locks(manager._lock) as acquired:
            if not acquired:
                raise _Incomplete('lock_busy')
            if manager._closing or getattr(manager, '_closed', False):
                raise _Incomplete('closing')
            owners = manager._owners
            if type(owners) is not dict:
                raise _Incomplete('invalid_observation')
            if agent in ('pi', 'hermes'):
                reserved = manager._reserved
                if type(reserved) not in (set, frozenset) or not reserved.issubset(owners):
                    raise _Incomplete('invalid_observation')
            for identity, owner in owners.items():
                selected = (owner.reserved if agent == 'codex' else identity in reserved
                            if agent in ('pi', 'hermes') else owner._slot_reserved)
                if type(selected) is not bool:
                    raise _Incomplete('invalid_observation')
                if selected:
                    entries.append((chat_id(identity), owner, owner.holder if agent == 'codex' else owner))
                    if len(entries) > limit:
                        raise _Incomplete('connection_limit')
    specialist = None
    if agent == 'codex':
        with try_locks(service._specialist_lock) as acquired:
            if not acquired:
                raise _Incomplete('lock_busy')
            specialist = service.specialist
    return manager, tuple(entries), specialist


def _same(before, after):
    manager, entries, specialist = before
    current, rows, holder = after
    if (manager is not current or specialist is not holder or len(entries) != len(rows)
            or any(a[0] != b[0] or a[1] is not b[1] or a[2] is not b[2]
                   for a, b in zip(entries, rows))):
        raise _Incomplete('registry_changed')


def _observe(agent, manager, entries, specialist):
    rows = []
    for identity, owner, holder in entries:
        if agent == 'codex':
            with try_locks(owner.lock) as acquired:
                if not acquired:
                    raise _Incomplete('lock_busy')
                value = _raw(holder.observe_work())
        else:
            options = ({'catalogue_identity': manager._maintenance_catalogue_identity}
                       if agent == 'hermes' else {})
            value = _raw(native_maintenance.observe_work(holder, agent, **options))
        if not value['complete']:
            raise _Incomplete(value['reason'] or 'observation_unavailable')
        rows.append(dict(agent=agent, role='chat', chat_id=identity, owner='rabbit', **value))
    if specialist is not None:
        value = _raw(specialist.observe_work())
        if value['connection_state'] != 'not_open':
            if not value['complete']:
                raise _Incomplete(value['reason'] or 'observation_unavailable')
            rows.append(dict(agent=agent, role='specialist', chat_id=None, owner='rabbit', **value))
    return rows


def snapshot(service):
    """Best effort bounded cache snapshot; one uncertain Agent stays unknown."""
    agents, works = [], []
    for agent, attribute, limit in _AGENTS:
        enabled = getattr(service, attribute, None) is not None or (
            agent == 'codex' and getattr(service, 'specialist', None) is not None)
        reason = None
        try:
            before = _capture(service, agent, attribute, limit)
            first = _observe(agent, *before)
            current = _observe(agent, *before)
            # No private holder fields are inspected here. A different epoch or
            # native identity invalidates the whole Agent's earlier observation.
            if (len(first) != len(current) or any(
                    a['epoch'] != b['epoch'] or a['native_session_id'] != b['native_session_id']
                    for a, b in zip(first, current))):
                raise _Incomplete('connection_changed')
            _same(before, _capture(service, agent, attribute, limit))
            works.extend(current)
        except _Incomplete as error:
            reason = str(error)
        except Exception:
            reason = 'observation_unavailable'
        agents.append({'agent': agent, 'enabled': enabled, 'complete': reason is None, 'reason': reason})
    return {'schema_version': 1, 'workspace_id': service.workspace_id,
            'scope': 'rabbit_owned_cache', 'snapshot_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'external_work': 'not_observed', 'complete': all(row['complete'] for row in agents),
            'agents': agents, 'works': works}
