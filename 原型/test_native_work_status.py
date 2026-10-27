"""Work observations use fictional caches; no Agent, RPC, store, or transcript."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import native_work_status as status


def raw(**changes):
    return dict({'complete': True, 'reason': None, 'status': 'idle',
                 'connection_state': 'ready', 'epoch': 'a' * 32,
                 'native_session_id': str(uuid.uuid4()), 'pending_requests': 0}, **changes)


class Holder:
    def __init__(self, value=None):
        self.value = value or raw()
        self.observe_work = Mock(side_effect=lambda: deepcopy(self.value))
        self._slot_reserved = True
        self.poison_reads = []

    def __getattr__(self, name):
        self.poison_reads.append(name)
        raise AssertionError('private transcript/RPC/store access: ' + name)


def service_fixture():
    service = SimpleNamespace(workspace_id='fictional-workspace', specialist=None,
        _specialist_lock=threading.RLock())
    entries = {}
    for agent, attribute, _limit in status._AGENTS:
        identity, holder = str(uuid.uuid4()), Holder()
        owner = SimpleNamespace(holder=holder, lock=threading.RLock(), reserved=True) if agent == 'codex' else holder
        manager = SimpleNamespace(_lock=threading.RLock(), _closing=False, _closed=False,
            _owners={identity: owner}, _reserved={identity}, _maintenance_catalogue_identity=(1, 2))
        setattr(service, attribute, manager)
        entries[agent] = (identity, manager, owner, holder)
    return service, entries


def native_observer(holder, agent, **options):
    return deepcopy(holder.value)


@contextmanager
def held_elsewhere(lock):
    ready, release = threading.Event(), threading.Event()
    def hold():
        with lock:
            ready.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    if not ready.wait(2):
        raise AssertionError('fictional lock not acquired')
    try:
        yield
    finally:
        release.set()
        worker.join(2)


class WorkStatusTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(status.native_maintenance, 'observe_work', side_effect=native_observer, create=True)
        self.observer = self.patch.start()
        self.addCleanup(self.patch.stop)

    def agent(self, result, agent):
        return next(row for row in result['agents'] if row['agent'] == agent)

    def test_fixed_metadata_shape_and_five_independent_agents(self):
        service, entries = service_fixture()
        result = status.snapshot(service)
        self.assertEqual(set(result), {'schema_version', 'workspace_id', 'scope', 'snapshot_at',
                                      'external_work', 'complete', 'agents', 'works'})
        self.assertTrue(result['complete'])
        self.assertEqual(result['scope'], 'rabbit_owned_cache')
        self.assertEqual(result['external_work'], 'not_observed')
        self.assertIsNotNone(datetime.fromisoformat(result['snapshot_at'].replace('Z', '+00:00')).tzinfo)
        self.assertEqual([row['agent'] for row in result['agents']], [row[0] for row in status._AGENTS])
        self.assertEqual(len(result['works']), 5)
        for row in result['works']:
            self.assertEqual(set(row), status._FIELDS | {'agent', 'role', 'chat_id', 'owner'})
            self.assertEqual(row['owner'], 'rabbit')
            self.assertEqual(row['role'], 'chat')
            self.assertEqual(row['status'], 'idle')
        for _identity, _manager, _owner, holder in entries.values():
            self.assertEqual(holder.poison_reads, [])
        hermes = entries['hermes'][3]
        self.assertEqual([call.kwargs for call in self.observer.call_args_list if call.args[0] is hermes],
                         [{'catalogue_identity': (1, 2)}] * 2)

    def test_empty_disabled_scope_does_not_mean_outside_agents_are_idle(self):
        service = SimpleNamespace(workspace_id='empty', specialist=None, _specialist_lock=threading.RLock())
        result = status.snapshot(service)
        self.assertTrue(result['complete'])
        self.assertEqual(result['works'], [])
        self.assertTrue(all(not row['enabled'] for row in result['agents']))
        self.observer.assert_not_called()

    def test_lazy_unreserved_holders_are_never_observed_or_counted_as_connections(self):
        service, entries = service_fixture()
        for agent, (identity, manager, owner, holder) in entries.items():
            for _ in range(5):
                lazy = Holder()
                lazy._slot_reserved = False
                manager._owners[str(uuid.uuid4())] = (SimpleNamespace(holder=lazy, reserved=False)
                                                     if agent == 'codex' else lazy)
        self.assertTrue(status.snapshot(service)['complete'])
        self.assertEqual(len(self.observer.call_args_list), 8)

    def test_liveness_is_not_inferred_from_a_process_and_delegated_work_is_preserved(self):
        service, entries = service_fixture()
        entries['pi'][3].value.update(status='running')
        entries['hermes'][3].value.update(status='needs_user', pending_requests=2)
        result = status.snapshot(service)
        rows = {row['agent']: row for row in result['works']}
        self.assertEqual(rows['codex']['status'], 'idle')
        self.assertEqual(rows['pi']['status'], 'running')
        self.assertEqual(rows['hermes']['pending_requests'], 2)

    def test_registry_and_specialist_locks_do_not_span_holder_observations(self):
        service, entries = service_fixture()
        service.specialist = Holder()
        def check_locks():
            obtained = []
            def probe():
                for lock in [entry[1]._lock for entry in entries.values()] + [service._specialist_lock]:
                    acquired = lock.acquire(False)
                    obtained.append(acquired)
                    if acquired: lock.release()
            worker = threading.Thread(target=probe)
            worker.start(); worker.join(2)
            self.assertEqual(obtained, [True] * 6)
        def observe(holder, agent, **options):
            check_locks()
            return deepcopy(holder.value)
        self.observer.side_effect = observe
        for holder in (entries['codex'][3], service.specialist):
            def codex_observe(holder=holder):
                check_locks()
                return deepcopy(holder.value)
            holder.observe_work.side_effect = codex_observe
        self.assertTrue(status.snapshot(service)['complete'])

    def test_busy_registry_or_owner_is_partial_and_never_returns_ready_for_that_agent(self):
        for target in ('registry', 'owner'):
            service, entries = service_fixture()
            _identity, manager, owner, _holder = entries['codex']
            with held_elsewhere(manager._lock if target == 'registry' else owner.lock):
                result = status.snapshot(service)
            self.assertFalse(result['complete'])
            self.assertEqual(self.agent(result, 'codex')['reason'], 'lock_busy')
            self.assertEqual({row['agent'] for row in result['works']}, {'pi', 'hermes', 'claude', 'dsh'})

    def test_replaced_registry_or_owner_or_holder_invalidates_sample(self):
        for replacement in ('manager', 'owner', 'holder', 'slot'):
            service, entries = service_fixture()
            identity, manager, owner, holder = entries['codex']
            def observe():
                if holder.observe_work.call_count == 2:
                    if replacement == 'manager': service.native_chats = SimpleNamespace(**vars(manager))
                    elif replacement == 'owner': manager._owners[identity] = SimpleNamespace(holder=holder, lock=threading.RLock(), reserved=True)
                    elif replacement == 'holder': owner.holder = Holder()
                    else: owner.reserved = False
                return deepcopy(holder.value)
            holder.observe_work.side_effect = observe
            result = status.snapshot(service)
            self.assertEqual(self.agent(result, 'codex')['reason'], 'registry_changed')
            self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))

    def test_changed_epoch_or_native_identity_is_not_current_ready_work(self):
        for field, value in (('epoch', 'b' * 32), ('native_session_id', str(uuid.uuid4()))):
            service, entries = service_fixture()
            holder = entries['codex'][3]
            holder.observe_work.side_effect = [deepcopy(holder.value), dict(holder.value, **{field: value})]
            result = status.snapshot(service)
            self.assertEqual(self.agent(result, 'codex')['reason'], 'connection_changed')
            self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))

    def test_slot_limit_invalid_reserved_registry_and_closing_are_bounded(self):
        for mutation, reason in (('limit', 'connection_limit'), ('reserved', 'invalid_observation'), ('closing', 'closing')):
            service, entries = service_fixture()
            identity, manager, _owner, _holder = entries['pi']
            if mutation == 'limit':
                extra = str(uuid.uuid4()); manager._owners[extra] = Holder(); manager._reserved.add(extra)
            elif mutation == 'reserved': manager._reserved.add(str(uuid.uuid4()))
            else: manager._closing = True
            result = status.snapshot(service)
            self.assertEqual(self.agent(result, 'pi')['reason'], reason)
            self.assertFalse(any(row['agent'] == 'pi' for row in result['works']))

    def test_specialist_is_independent_and_not_open_holder_is_not_a_work(self):
        service, _entries = service_fixture()
        service.native_chats = None
        service.specialist = Holder(raw(complete=False, reason='connection_unavailable', status='unknown',
                                       connection_state='not_open', epoch=None, native_session_id=None,
                                       pending_requests=None))
        result = status.snapshot(service)
        self.assertTrue(self.agent(result, 'codex')['enabled'])
        self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))
        service.specialist.value.update(complete=True, reason=None, status='idle', pending_requests=0,
                                        connection_state='ready', epoch='a' * 32, native_session_id=str(uuid.uuid4()))
        result = status.snapshot(service)
        row = next(row for row in result['works'] if row['agent'] == 'codex')
        self.assertEqual((row['role'], row['chat_id']), ('specialist', None))

    def test_specialist_replacement_or_lock_contention_drops_codex_only(self):
        service, _entries = service_fixture()
        service.specialist = holder = Holder()
        def observe():
            if holder.observe_work.call_count == 2: service.specialist = Holder()
            return deepcopy(holder.value)
        holder.observe_work.side_effect = observe
        result = status.snapshot(service)
        self.assertEqual(self.agent(result, 'codex')['reason'], 'registry_changed')
        with held_elsewhere(service._specialist_lock):
            result = status.snapshot(service)
        self.assertEqual(self.agent(result, 'codex')['reason'], 'lock_busy')
        self.assertTrue(self.agent(result, 'pi')['complete'])

    def test_unavailable_and_poisoned_payloads_are_sanitized_not_idle(self):
        for value in (raw(complete=False, reason='cache_unavailable', status='unknown'),
                      dict(raw(), transcript='private body'), raw(native_session_id='/private/token'),
                      raw(reason='private message with spaces'), raw(pending_requests=True),
                      raw(epoch='private'), raw(complete=1)):
            service, entries = service_fixture()
            entries['codex'][3].observe_work.return_value = value
            entries['codex'][3].observe_work.side_effect = None
            result = status.snapshot(service)
            self.assertFalse(self.agent(result, 'codex')['complete'])
            self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))
            self.assertNotIn('private', str(result))
        service, entries = service_fixture()
        entries['codex'][3].observe_work.side_effect = RuntimeError('private credential or body')
        result = status.snapshot(service)
        self.assertEqual(self.agent(result, 'codex')['reason'], 'observation_unavailable')
        self.assertNotIn('private', str(result))

    def test_idle_and_needs_user_require_consistent_evidence(self):
        invalid = (raw(complete=False, reason='observation_busy'), raw(connection_state='closed'),
                   raw(epoch=None), raw(native_session_id=None), raw(pending_requests=1),
                   raw(status='needs_user'), raw(status='needs_user', pending_requests=None),
                   raw(reason='a' * 49))
        for value in invalid:
            with self.subTest(value=value):
                service, entries = service_fixture()
                entries['codex'][3].observe_work.side_effect = None
                entries['codex'][3].observe_work.return_value = value
                result = status.snapshot(service)
                self.assertEqual(self.agent(result, 'codex')['reason'], 'invalid_observation')
                self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))

    def test_unknown_request_remains_unknown_and_unopened_specialist_lock_failure_is_partial(self):
        service, entries = service_fixture()
        entries['codex'][3].value.update(status='unknown', reason='request_unknown')
        result = status.snapshot(service)
        self.assertEqual(next(row for row in result['works'] if row['agent'] == 'codex')['status'], 'unknown')
        service.specialist = Holder(raw(complete=False, reason='lock_busy', status='unknown',
                                       connection_state='unknown', epoch=None, native_session_id=None,
                                       pending_requests=None))
        result = status.snapshot(service)
        self.assertEqual(self.agent(result, 'codex')['reason'], 'lock_busy')
        self.assertFalse(any(row['agent'] == 'codex' for row in result['works']))


if __name__ == '__main__':
    unittest.main()
