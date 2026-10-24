"""Opaque Codex model-directory observations over an already owned holder.

The host owns catalogue registration and supplies its short registry guard.
This adapter never opens a connection, sends RPC or exposes locks/transports.
"""
from contextlib import contextmanager
import re
import threading

from .maintenance import try_locks


_EPOCH = re.compile(r'[0-9a-f]{32}\Z')


class ModelObservationError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if reason in ('busy', 'changed', 'unavailable') else 'unavailable'
        super().__init__('Model directory observation: ' + self.reason)


class _Snapshot:
    __slots__ = ('_holder', '_identity')

    def __init__(self, holder, identity):
        # Retain object references so replacing a transport/reader/session can
        # never compare equal through a reused integer identity or custom __eq__.
        self._holder, self._identity = holder, identity

    @property
    def epoch(self):
        return self._identity[0]

    def matches(self, holder, identity):
        previous = self._identity
        return (self._holder is holder and identity is not None
                and previous[0] == identity[0]
                and all(previous[index] is identity[index] for index in (1, 2, 3))
                and previous[4:] == identity[4:])

    def __repr__(self):
        return '<opaque Codex model observation>'


class CodexModelObservation:
    """Hold only the action guard across a model read; observe short state twice.

    ``registry()`` must acquire the host's nonblocking manager lock and verify
    that the original owner remains registered. ``remember()`` saves only the
    resulting catalogue and runs inside that final guard, after identity checks.
    Neither callback may perform native I/O.
    """
    def __init__(self, holder):
        self.__holder = holder
        self.__action_thread = None

    @contextmanager
    def action(self):
        with try_locks(self.__holder._actions) as acquired:
            if not acquired:
                raise ModelObservationError('busy')
            previous = self.__action_thread
            self.__action_thread = threading.get_ident()
            try:
                yield
            finally:
                self.__action_thread = previous

    def observe(self, *, registry, expected=None, remember=None):
        if self.__action_thread != threading.get_ident():
            raise ModelObservationError('unavailable')
        if remember is not None and expected is None:
            raise ModelObservationError('changed')
        holder = self.__holder
        # The action guard prevents lifecycle replacement while selecting the
        # short cache/session locks. No such short lock spans a native request.
        session = holder._session
        locks = (holder._cache,) + ((session._lock,) if session is not None else ())
        with try_locks(*locks) as acquired:
            if not acquired:
                raise ModelObservationError('busy')
            with registry():
                identity = self.__identity_locked(session)
                if expected is not None and (type(expected) is not _Snapshot
                        or not expected.matches(holder, identity)):
                    raise ModelObservationError('changed')
                if remember is not None:
                    remember()
                return None if identity is None else _Snapshot(holder, identity)

    def __identity_locked(self, session):
        holder = self.__holder
        epoch = holder._epoch
        if (holder._state != 'ready' or getattr(holder, '_closed', False)
                or holder._rpc is None or type(epoch) is not str or not _EPOCH.fullmatch(epoch)
                or holder._reader is None or not holder._reader.is_alive()
                or holder._session is not session or session is None or not session._initialized):
            return None
        # A ready connection may have no thread yet. Listing models must leave
        # that null binding intact, rather than creating or loading a thread.
        return (epoch, holder._rpc, holder._reader, session,
                session._bound.get('session_id'), session._bound.get('thread_id'))
