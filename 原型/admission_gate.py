"""Process-local work admission; no I/O, task bodies or cleanup inference.

One gate belongs to one Service instance. A lease is a short-locked admission
decision, not a mutex held throughout business work. Reserve a child BEFORE
publishing accepted asynchronous work; hand it off exactly once in that worker.

Maintenance identity is (maintenance_id, token, generation). A fresh attempt
uses state()['generation'] + 1. Repeating the same identity returns the same
outcome without probing again. Only the latest attempt is retained; an older
generation is stale after another attempt. There is no timeout or auto-reopen.
"""
import re
import secrets
import threading
import time


KINDS = frozenset(('http', 'service', 'native', 'worker', 'memory', 'repair'))
MAX_GENERATION = (1 << 53) - 1
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_COUNT = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')


class AdmissionClosed(RuntimeError):
    """This attempt did not receive admission; not a prior request's receipt."""
    def __init__(self, code, state):
        self.code, self.state = code, state
        super().__init__(code)


class AdmissionBusy(RuntimeError):
    """Maintenance did not become prepared; existing work was left alone."""
    def __init__(self, code, state):
        self.code, self.state = code, state
        self.counts = dict(state['busy'])
        super().__init__(code)


def _identifier(value, *, token=False):
    if (type(value) is not str or not _ID.fullmatch(value)
            or token and len(value) < 16):
        raise ValueError('Invalid admission identity.')
    return value


def _generation(value):
    if type(value) is not int or not 1 <= value <= MAX_GENERATION:
        raise ValueError('Invalid maintenance generation.')
    return value


def _kind(value):
    if type(value) is not str or value not in KINDS:
        raise ValueError('Invalid admission kind.')
    return value


def _probe_counts(value):
    if (type(value) is not dict or len(value) > 32 or any(
            type(key) is not str or not _COUNT.fullmatch(key)
            or type(count) is not int or not 0 <= count <= MAX_GENERATION
            for key, count in value.items())):
        raise ValueError('A bounded structural count map is required.')
    return {key: value[key] for key in sorted(value) if value[key]}


class _Lease:
    def __init__(self, gate, kind, reservation=None):
        self._gate, self._kind, self._reservation = gate, kind, reservation
        self._entered = self._closed = self._owns = False
        self._activity = self._thread = None

    def __enter__(self):
        gate = self._gate
        with gate._lock:
            if self._entered:
                raise ValueError('A lease cannot be reused.')
            stack = gate._stack()
            if self._reservation is not None:
                if self._reservation._state != 'reserved':
                    raise ValueError('The child reservation is unavailable.')
                self._activity, self._owns = self._reservation._activity, True
                gate._activities[self._activity]['state'] = 'active'
                self._reservation._state = 'active'
            else:
                if not stack and gate._mode != 'running':
                    raise AdmissionClosed('maintenance_closed', gate._snapshot())
                if stack:
                    self._activity = stack[-1]._activity
                else:
                    self._activity, self._owns = gate._new_activity(self._kind, 'active'), True
            self._entered, self._thread = True, threading.get_ident()
            stack.append(self)
            return self

    def __exit__(self, *_error):
        gate = self._gate
        with gate._lock:
            stack = gate._stack()
            if (not self._entered or self._closed or self._thread != threading.get_ident()
                    or not stack or stack[-1] is not self):
                raise ValueError('Leases must leave their owning thread in stack order.')
            stack.pop(); self._closed = True
            if self._owns:
                del gate._activities[self._activity]
                if self._reservation is not None:
                    self._reservation._state = 'released'
                gate._changed.notify_all()
        return False

    def reserve(self, kind='worker'):
        """Count a child now, before its thread or accepted work is published."""
        kind = _kind(kind)
        gate = self._gate
        with gate._lock:
            if (not self._entered or self._closed or self._thread != threading.get_ident()
                    or self not in gate._stack() or self._activity not in gate._activities):
                raise ValueError('A live parent lease is required.')
            return _Reservation(gate, kind, gate._new_activity(kind, 'reserved'))


class _Reservation:
    def __init__(self, gate, kind, activity):
        self._gate, self._kind, self._activity = gate, kind, activity
        self._state = 'reserved'

    def handoff(self):
        """Enter once in the child thread (or synchronously in the same thread)."""
        return _Lease(self._gate, self._kind, self)

    def cancel(self):
        """Release an unstarted child; never cancel a worker already handed off."""
        with self._gate._lock:
            if self._state == 'cancelled':
                return False
            if self._state != 'reserved':
                raise ValueError('A handed-off child cannot be cancelled here.')
            del self._gate._activities[self._activity]
            self._state = 'cancelled'
            self._gate._changed.notify_all()
            return True


class AdmissionGate:
    def __init__(self, instance_id):
        self.instance_id = _identifier(instance_id)
        self._lock, self._local = threading.RLock(), threading.local()
        self._changed = threading.Condition(self._lock)
        self._mode, self._generation = 'running', 0
        self._activities, self._serial, self._maintenance = {}, 0, None

    def _stack(self):
        if not hasattr(self._local, 'stack'):
            self._local.stack = []
        return self._local.stack

    def _new_activity(self, kind, state):
        self._serial += 1
        self._activities[self._serial] = {'kind': kind, 'state': state}
        return self._serial

    def _snapshot(self):
        current = self._maintenance
        return {'instance_id': self.instance_id, 'state': self._mode,
            'generation': self._generation,
            'maintenance_id': current['id'] if current else None,
            'phase': current['phase'] if current else None,
            'leases': {'total': len(self._activities),
                'active': sum(row['state'] == 'active' for row in self._activities.values()),
                'reserved': sum(row['state'] == 'reserved' for row in self._activities.values()),
                'by_kind': {kind: sum(row['kind'] == kind for row in self._activities.values())
                            for kind in sorted(KINDS) if any(row['kind'] == kind for row in self._activities.values())}},
            'busy': dict(current['busy']) if current else {}}

    def _match(self, identity):
        current = self._maintenance
        return (current is not None and current['id'] == identity[0]
            and current['generation'] == identity[2]
            and secrets.compare_digest(current['token'], identity[1]))

    @staticmethod
    def _identity(maintenance_id, token, generation):
        return (_identifier(maintenance_id), _identifier(token, token=True), _generation(generation))

    def _checked(self, identity):
        if not self._match(identity):
            raise AdmissionClosed('maintenance_mismatch', self._snapshot())
        return self._maintenance

    def lease(self, kind='service'):
        return _Lease(self, _kind(kind))

    def state(self, maintenance_id=None, token=None, generation=None):
        """Finite local state; omit all identity arguments or supply all three."""
        identity = None
        if any(value is not None for value in (maintenance_id, token, generation)):
            identity = self._identity(maintenance_id, token, generation)
        with self._lock:
            if identity is not None:
                self._checked(identity)
            return self._snapshot()

    def begin_shutdown(self):
        """Trusted owner only: block new callers while accepted work drains."""
        with self._lock:
            self._mode = 'closing'
            self._changed.notify_all()
            return self._snapshot()

    def wait_for_idle(self, timeout):
        """Wait only for admission leases, never infer native process cleanup."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 60:
            raise ValueError('Invalid admission wait.')
        deadline = time.monotonic() + timeout
        with self._changed:
            if self._stack():
                raise ValueError('An admitted caller cannot wait for itself.')
            return self._changed.wait_for(lambda: not self._activities,
                timeout=max(0, deadline - time.monotonic()))

    def _abort(self, code, busy):
        self._mode = 'running'
        self._maintenance.update(phase='probe_failed' if code == 'probe_failed' else 'busy',
                                 code=code, busy=busy)
        return AdmissionBusy(code, self._snapshot())

    def prepare(self, maintenance_id, token, generation, *, probe):
        """Close admission before a zero-lease, out-of-lock local-only probe."""
        identity = self._identity(maintenance_id, token, generation)
        if not callable(probe):
            raise ValueError('A structural maintenance probe is required.')
        with self._lock:
            if self._match(identity):
                if self._maintenance['phase'] in ('busy', 'probe_failed'):
                    raise AdmissionBusy(self._maintenance['code'], self._snapshot())
                return self._snapshot()
            if self._mode != 'running':
                raise AdmissionClosed('maintenance_closed', self._snapshot())
            if generation != self._generation + 1:
                raise AdmissionClosed('stale_generation', self._snapshot())
            self._generation, self._mode = generation, 'preparing'
            self._maintenance = {'id': maintenance_id, 'token': token, 'generation': generation,
                                 'phase': 'preparing', 'busy': {}}
            if self._activities:
                raise self._abort('active_leases', {'leases': len(self._activities)})
        try:
            counts = _probe_counts(probe())
        except BaseException as error:
            with self._lock:
                failure = (AdmissionClosed('already_closing', self._snapshot()) if self._mode == 'closing'
                           else self._abort('probe_failed', {}))
            if isinstance(error, Exception):
                raise failure from None
            raise
        with self._lock:
            # Admission stayed closed for the entire probe; no mutex was held
            # while the caller inspected its own component locks.
            if self._mode == 'closing':
                raise AdmissionClosed('already_closing', self._snapshot())
            if counts:
                raise self._abort('probe_busy', counts)
            if self._activities:
                raise self._abort('active_leases', {'leases': len(self._activities)})
            self._mode = 'prepared'
            self._maintenance['phase'] = 'prepared'
            return self._snapshot()

    def cancel(self, maintenance_id, token, generation):
        identity = self._identity(maintenance_id, token, generation)
        with self._lock:
            current = self._checked(identity)
            if self._mode == 'closing':
                raise AdmissionClosed('already_closing', self._snapshot())
            if current['phase'] == 'preparing':
                raise AdmissionBusy('probe_in_progress', self._snapshot())
            if current['phase'] == 'prepared':
                self._mode, current['phase'] = 'running', 'cancelled'
            return self._snapshot()

    def commit(self, maintenance_id, token, generation):
        """Keep admission closed; the caller still owes actual drain/cleanup."""
        identity = self._identity(maintenance_id, token, generation)
        with self._lock:
            current = self._checked(identity)
            if current['phase'] == 'closing':
                return self._snapshot()
            if self._mode == 'closing':
                raise AdmissionClosed('already_closing', self._snapshot())
            if current['phase'] == 'preparing':
                raise AdmissionBusy('probe_in_progress', self._snapshot())
            if current['phase'] != 'prepared':
                raise AdmissionClosed('maintenance_not_prepared', self._snapshot())
            self._mode, current['phase'] = 'closing', 'closing'
            return self._snapshot()
