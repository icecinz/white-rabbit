"""Host aggregation of local runtime maintenance observations.

Codex execution owners and ledgers expose their own public metadata snapshots.
Other native owners remain host adapters. No observation replaces admission.
"""
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
