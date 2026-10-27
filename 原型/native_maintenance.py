"""Host aggregation of local runtime maintenance observations.

Codex execution owners and ledgers expose their own public metadata snapshots.
Other native owners remain host adapters. No observation replaces admission.
"""
import re

# Preserve the existing metadata-test seams; these are the same stdlib module
# objects used by the single helper implementation in rabbit_runtime.
from rabbit_runtime.maintenance import (LIMIT, COUNT_NAMES, try_locks, file_identity,
    sidecar_identities as _sidecars, count_rows, merge_observation as _merge,
    sqlite3, time)


def _requests(holder, counts):
    for row in holder._requests.values():
        state = row['state']
        if state == 'pending': counts['pending_requests'] += 1
        elif state == 'unknown': counts['unknown_requests'] += 1


def _holder(holder, kind, result):
    locks = [holder._action, holder._cache]
    if kind == 'dsh': locks.append(holder._settle_lock)
    with try_locks(*locks) as acquired:
        if not acquired: result['complete'] = False; return
        counts = result['counts']
        counts['rpc_retained'] += int(holder._rpc is not None)
        counts['readers_alive'] += int(holder._reader is not None and holder._reader.is_alive())
        active = holder._state == 'opening' or holder._active is not None
        _requests(holder, counts)
        if kind == 'pi':
            state = holder._native_state or {}
            active = active or bool(state.get('isStreaming') or state.get('isCompacting') or state.get('pendingMessageCount'))
            counts['continuation_jobs'] += len(holder._continuations)
            counts['configuration_pending'] += int(holder._pending_verified is not None)
        elif kind == 'hermes':
            state = holder._native_state or {}
            active = active or bool(state.get('running') or state.get('queued') or state.get('inflight'))
            counts['pending_requests'] += int(holder._native_pending is not None)
            counts['pending_requests'] += len(holder._native_approvals)+int(holder._native_approval_current is not None)
        counts['active'] += int(active)


def _ledger(store, kind):
    if kind in ('codex', 'specialist', 'dsh', 'claude', 'hermes'):
        return store.maintenance_state()
    if kind in ('pi', 'hermes'):
        specs = {'pending_prompts':(kind+'_operations', "status IN ('submitted_unknown','accepted')", ())}
    else:
        specs = {'pending_creations':(kind+'_creations', "status='submitted_unknown'", ()),
            'pending_prompts':(kind+'_prompts', "status IN ('submitted_unknown','accepted')", ()),
            'unknown_controls':(kind+'_controls', "status='submitted_unknown'", ())}
    return count_rows(store.path, specs, lock=store.lock, expected_identity=store._file_identity,
        check_path=store._check_path)


def maintenance_state(manager, kind):
    result = {'complete':True, 'counts':dict.fromkeys(COUNT_NAMES, 0)}
    try:
        if kind == 'specialist':
            _merge(result, manager.maintenance_state())
            result['counts'] = {name:min(LIMIT, value) for name, value in result['counts'].items()}
            return result
        with try_locks(manager._lock) as acquired:
            if not acquired: result['complete'] = False; return result
            owners = list(manager._owners.values())
            result['counts']['closing'] = int(manager._closing)
            result['counts']['reserved'] = (sum(owner.reserved for owner in owners) if kind == 'codex' else
                len(manager._reserved) if kind in ('pi', 'hermes') else sum(owner._slot_reserved for owner in owners))
        for owner in owners:
            if kind == 'codex':
                with try_locks(owner.lock) as acquired:
                    if acquired: _merge(result, owner.holder.maintenance_state(include_ledger=False))
                    else: result['complete'] = False
            else: _holder(owner, kind, result)
        # Codex owners share this store; observe its ledger exactly once.
        _merge(result, _ledger(manager.store, kind))
        if kind == 'hermes':
            specs = {'pending_profile_applications':('hermes_profile_applications',
                "workspace_id=? AND phase NOT IN ('verified','failed')", (manager.workspace_id,)),
                'pending_native_drafts':('hermes_native_drafts', "workspace_id=? AND phase!='verified'", (manager.workspace_id,)),
                'pending_attachments':('hermes_attachment_transactions',
                    "workspace_id=? AND phase NOT IN ('consumed','discarded')", (manager.workspace_id,))}
            _merge(result, count_rows(manager.catalogue.path, specs,
                expected_identity=manager._maintenance_catalogue_identity,
                optional_tables=() if manager.profiles_enabled else
                    ('hermes_profile_applications', 'hermes_native_drafts', 'hermes_attachment_transactions')))
    except Exception:
        result['complete'] = False
    result['counts'] = {name:min(LIMIT, value) for name, value in result['counts'].items()}
    return result


_WORK_KINDS = frozenset(('pi', 'hermes', 'claude', 'dsh'))
_WORK_CONNECTIONS = frozenset(('not_open', 'opening', 'ready', 'failed', 'closed'))
_WORK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')
_WORK_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_WORK_EPOCH = re.compile(r'[0-9a-f]{32}\Z')


def observe_work(holder, kind, *, catalogue_identity=None):
    """Observe one existing host owner, without RPC, view() or native reads.

    ``idle`` means no Rabbit-known unfinished work in this owned cache. It is
    not a native-background-work claim. Timestamps/freshness belong to the host.
    Unknown durable submissions, incomplete metadata and lock contention never
    become idle. Request payloads, paths and model settings are not projected.
    """
    result = dict(complete=False, reason='metadata_unavailable', status='unknown',
        connection_state='unknown', epoch=None, native_session_id=None, pending_requests=None)
    if type(kind) is not str or kind not in _WORK_KINDS:
        return dict(result, reason='unsupported_agent')
    try:
        locks = [holder._action, holder._cache]
        if kind == 'dsh': locks.append(holder._settle_lock)
        with try_locks(*locks) as acquired:
            if not acquired: return dict(result, reason='observation_busy')
            state, epoch, identity = holder._state, holder._epoch, holder.identity
            if state not in _WORK_CONNECTIONS or type(epoch) is not str or not _WORK_EPOCH.fullmatch(epoch):
                return result
            result.update(connection_state=state, epoch=epoch)
            if type(identity) is not str or not _WORK_UUID.fullmatch(identity): return result
            requests = holder._requests
            if not isinstance(requests, dict) or len(requests) > 128: return result
            pending = unknown = 0
            for row in requests.values():
                if type(row) is not dict or row.get('state') not in ('pending', 'unknown', 'answered', 'expired'):
                    return result
                pending += row['state'] == 'pending'
                unknown += row['state'] == 'unknown'
            result['pending_requests'] = pending
            native = holder._native_state if kind in ('pi', 'hermes') else holder._info
            if kind == 'pi':
                live = holder._live_identity
                native_id = live.get('session_id') if type(live) is dict else None
            elif kind == 'hermes': native_id = holder._live_key
            elif kind == 'claude': native_id = native.get('configured_session_id') if type(native) is dict else None
            else: native_id = native.get('session_id') if type(native) is dict else None
            if type(native_id) is str and _WORK_ID.fullmatch(native_id):
                result['native_session_id'] = native_id

            # The shared helper reads only capped counts from fixed predicates,
            # with the store's original inode pin and nonblocking path guard.
            table = kind + ('_operations' if kind in ('pi', 'hermes') else '_prompts')
            specs = {'unknown_prompts': (table, "chat_id=? AND status='submitted_unknown'", (identity,)),
                     'accepted_prompts': (table, "chat_id=? AND status='accepted'", (identity,))}
            if kind in ('claude', 'dsh'):
                specs.update(pending_creations=(kind+'_creations', "chat_id=? AND status='submitted_unknown'", (identity,)),
                             unknown_controls=(kind+'_controls', "chat_id=? AND status='submitted_unknown'", (identity,)))
            store = holder.store
            ledger = count_rows(store.path, specs, lock=store.lock,
                expected_identity=store._file_identity, check_path=store._check_path)
            if not ledger['complete']: return dict(result, reason='ledger_unavailable')
            counts = ledger['counts']
            configuration_pending = False
            if kind == 'hermes':
                profiles = holder.profile_intents
                if (profiles is None or profiles.workspace_id != holder.workspace_id
                        or type(catalogue_identity) is not tuple or len(catalogue_identity) != 2
                        or any(type(value) is not int or value < 0 for value in catalogue_identity)):
                    return dict(result, reason='configuration_unavailable')
                fields = (holder.workspace_id, identity)
                extra = {'applications': ('hermes_profile_applications',
                    "workspace_id=? AND chat_id=? AND phase NOT IN ('verified','failed')", fields),
                    'drafts': ('hermes_native_drafts', "workspace_id=? AND chat_id=? AND phase!='verified'", fields),
                    'attachments': ('hermes_attachment_transactions',
                    "workspace_id=? AND chat_id=? AND phase NOT IN ('consumed','discarded')", fields)}
                observed = count_rows(profiles.path, extra, expected_identity=catalogue_identity,
                    optional_tables=('hermes_attachment_transactions',) if holder.attachments is None else ())
                if not observed['complete']: return dict(result, reason='configuration_unavailable')
                configuration_pending = any(observed['counts'].values())
                # These native lists can overlap the normalized requests. The
                # count is a conservative notice count, not distinct RPC IDs.
                pending = max(pending, len(holder._native_approvals)
                    + int(holder._native_pending is not None) + int(holder._native_approval_current is not None))
                result['pending_requests'] = min(LIMIT, pending)
            elif kind == 'pi': configuration_pending = holder._pending_verified is not None
            if unknown or counts['unknown_prompts'] or counts.get('unknown_controls') or counts.get('pending_creations'):
                return dict(result, complete=True, reason='request_unknown')
            if state != 'ready':
                if state == 'opening': return dict(result, complete=True, reason='opening', status='running')
                return dict(result, complete=True, reason=state if state in ('not_open', 'closed') else 'connection_unavailable')
            if (holder._rpc is None or holder._reader is None or not holder._reader.is_alive()
                    or holder._stop.is_set()):
                return dict(result, reason='connection_unavailable')
            if result['native_session_id'] is None: return dict(result, reason='identity_unavailable')
            if type(native) is not dict: return dict(result, reason='state_unavailable')
            busy = holder._active is not None or bool(counts['accepted_prompts'])
            if kind == 'pi':
                if native.get('sessionId') != result['native_session_id']:
                    return dict(result, reason='identity_unavailable')
                if (type(native.get('isStreaming')) is not bool or type(native.get('isCompacting')) is not bool
                        or type(native.get('pendingMessageCount')) is not int or native['pendingMessageCount'] < 0):
                    return dict(result, reason='state_unavailable')
                busy |= bool(native['isStreaming'] or native['isCompacting'] or native['pendingMessageCount'] or holder._continuations)
            elif kind == 'hermes':
                if type(native.get('running')) is not bool: return dict(result, reason='state_unavailable')
                if native.get('session_id') != holder._runtime or native.get('session_key') != holder._live_key:
                    return dict(result, reason='identity_unavailable')
                busy |= bool(native['running'] or native.get('queued') or native.get('inflight'))
            elif kind == 'dsh':
                if native.get('live') is not True or type(native.get('running')) is not bool:
                    return dict(result, reason='state_unavailable')
                busy |= native['running']
            if pending: return dict(result, complete=True, reason=None, status='needs_user')
            if configuration_pending: return dict(result, complete=True, reason='configuration_pending')
            return dict(result, complete=True, reason=None, status='running' if busy else 'idle')
    except Exception:
        return result
