"""Bounded native Pi JSONL transport; no session, model or tool policy.

Only response frames are correlated. Events, including extension UI requests
with their own IDs, retain the native shape. Diagnostics never include payloads
or stderr. The owner must close the connection; close never sends abort.
"""
from collections import deque
from dataclasses import dataclass, field
import json
import math
import os
import select
import signal
import subprocess
import threading
import time
import uuid

from .material_protocol import MAX_RPC_FRAME_BYTES, MAX_RPC_EVENT_BYTES


class PiRpcError(Exception):
    def __init__(self, reason='transport_failed', *, result_unknown=False):
        super().__init__('Pi RPC transport failed (' + reason + ').')
        self.reason = reason
        self.code = reason
        self.result_unknown = result_unknown


class TransportError(PiRpcError):
    pass


class ProtocolError(TransportError):
    pass


class SubmittedTimeout(TransportError):
    def __init__(self):
        super().__init__('submitted_timeout', result_unknown=True)


class KnownUnsentTimeout(TransportError):
    def __init__(self):
        super().__init__('not_sent_timeout', result_unknown=False)


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('Timeout must be positive and finite.')
    return float(value)


def _json_value(value):
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode('utf-8', 'strict')
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError('JSON keys must be strings.')
            key.encode('utf-8', 'strict')
            _json_value(item)
        return
    raise ValueError('Only finite JSON values are accepted.')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key.')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('Nonfinite JSON constant.')


def _identifier(value):
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError('Pi request IDs must be nonempty bounded strings.')
    value.encode('utf-8', 'strict')
    return value


@dataclass
class _Pending:
    command: str
    frame_size: int = 0
    written: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    response: object = None
    error: object = None


class PiRpc:
    def __init__(self, command, cwd=None, *, max_frame_bytes=MAX_RPC_FRAME_BYTES,
                 max_events=1024, max_event_bytes=MAX_RPC_EVENT_BYTES):
        if (not isinstance(command, (list, tuple)) or not command
                or any(type(part) is not str or not part or '\x00' in part for part in command)):
            raise ValueError('A literal executable argv is required.')
        for bound in (max_frame_bytes, max_events, max_event_bytes):
            if type(bound) is not int or bound <= 0:
                raise ValueError('Transport bounds must be positive integers.')
        self._command, self._cwd = tuple(command), cwd
        self._max_frame_bytes, self._max_events = max_frame_bytes, max_events
        self._max_event_bytes = max_event_bytes
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._write_lock = threading.Lock()
        self._stop_io = threading.Event()
        self._cleanup_done = threading.Event()
        self._events, self._event_bytes, self._pending = deque(), 0, {}
        self._used_ids = set()
        self._prefix, self._next_id = uuid.uuid4().hex + '-', 0
        self._process, self._owned_pgid, self._threads = None, None, []
        self._started, self._closing, self._failure = False, False, None
        self._cleanup_started, self._cleanup_error = False, None
        self._event_consumer = None
        self._stderr_bytes, self._stderr_saturated, self._ignored_responses = 0, False, 0
        self._events_received = 0
        self._shutdown_method = None

    @property
    def started(self):
        with self._lock:
            return self._started

    @property
    def running(self):
        with self._lock:
            return bool(self._process is not None and self._failure is None
                        and self._process.poll() is None)

    @property
    def stats(self):
        with self._lock:
            return {'pid': self._process.pid if self._process else None,
                    'pending_requests': len(self._pending), 'queued_events': len(self._events),
                    'queued_event_bytes': self._event_bytes, 'stderr_bytes': self._stderr_bytes,
                    'events_received': self._events_received,
                    'stderr_saturated': self._stderr_saturated, 'ignored_responses': self._ignored_responses,
                    'live_io_threads': sum(t.is_alive() for t in self._threads),
                    'io_threads_stopped': all(not t.is_alive() for t in self._threads),
                    'process_exited': self._process.poll() is not None if self._process else True,
                    'cleanup_complete': self._cleanup_done.is_set() and self._cleanup_error is None,
                    'shutdown_method': self._shutdown_method}

    def _check(self):
        if self._failure:
            raise type(self._failure)(self._failure.reason) from None
        if not self._started:
            raise TransportError('not_started')

    def start(self):
        with self._lock:
            if self._failure:
                raise TransportError(self._failure.reason)
            if self._started:
                return self
            try:
                self._process = subprocess.Popen(self._command, cwd=self._cwd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, shell=False,
                    start_new_session=os.name == 'posix')
                if os.name == 'posix':
                    self._owned_pgid = os.getpgid(self._process.pid)
                    if self._owned_pgid != self._process.pid:
                        raise OSError('Owned group unavailable.')
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    os.set_blocking(stream.fileno(), False)
                self._started = True
                for name, target in (('stdout', self._stdout_loop), ('stderr', self._stderr_loop)):
                    thread = threading.Thread(target=target, name='pi-rpc-' + name, daemon=True)
                    thread.start()
                    self._threads.append(thread)
            except (OSError, ValueError, RuntimeError):
                self._fail(TransportError('start_failed'))
                raise TransportError('start_failed') from None
        return self

    def _frame(self, value):
        _json_value(value)
        frame = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
        if len(frame) > self._max_frame_bytes:
            raise ValueError('Pi frame exceeds the configured bound.')
        return frame

    def _write(self, frame, pending, deadline):
        if not self._write_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise KnownUnsentTimeout()
        view = None
        try:
            view = memoryview(frame)
            while pending.written < len(frame):
                with self._lock:
                    if self._failure:
                        raise TransportError(self._failure.reason, result_unknown=bool(pending.written))
                    self._check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if pending.written:
                        self._fail(TransportError('incomplete_write_timeout', result_unknown=True))
                        raise SubmittedTimeout()
                    raise KnownUnsentTimeout()
                try:
                    if not select.select([], [self._process.stdin], [], min(0.05, remaining))[1]:
                        continue
                    with self._lock:
                        if self._failure:
                            raise TransportError(self._failure.reason, result_unknown=bool(pending.written))
                        # Nonblocking pipes may accept only a small fragment.
                        # Keep one frame and zero-copy views of its remainder.
                        count = os.write(self._process.stdin.fileno(), view[pending.written:])
                        if count <= 0:
                            raise OSError('No write progress.')
                        pending.written += count
                except BlockingIOError:
                    continue
                except (OSError, ValueError):
                    error = TransportError('stdin_write_failed', result_unknown=bool(pending.written))
                    self._fail(error)
                    raise error from None
        finally:
            if view is not None:
                view.release()
            self._write_lock.release()

    def request(self, command, fields=None, *, timeout=20, request_id=None):
        if type(command) is not str or not command or command == 'extension_ui_response':
            raise ValueError('A native Pi command type is required.')
        fields = {} if fields is None else fields
        if type(fields) is not dict or 'id' in fields or 'type' in fields:
            raise ValueError('Command fields cannot replace type or ID.')
        deadline = time.monotonic() + _timeout(timeout)
        with self._lock:
            self._check()
            if len(self._pending) >= self._max_events or len(self._used_ids) >= 65536:
                raise TransportError('request_limit')
            if request_id is None:
                self._next_id += 1
                request_id = self._prefix + str(self._next_id)
            else:
                _identifier(request_id)
                if request_id.startswith(self._prefix):
                    raise ValueError('The automatic ID namespace is reserved.')
            if request_id in self._used_ids:
                raise ValueError('Pi request IDs cannot be reused on a connection.')
            frame = self._frame(dict(fields, type=command, id=request_id))
            pending = _Pending(command, frame_size=len(frame))
            self._used_ids.add(request_id)
            self._pending[request_id] = pending
        try:
            self._write(frame, pending, deadline)
            pending.done.wait(max(0, deadline - time.monotonic()))
            with self._lock:
                if pending.done.is_set():
                    if pending.error:
                        raise pending.error
                    return pending.response
                raise SubmittedTimeout()
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def send_ui_response(self, request_id, result, *, timeout=20):
        """One unacknowledged write; the controller owns interaction permission."""
        _identifier(request_id)
        if type(result) is not dict or 'id' in result or 'type' in result:
            raise ValueError('UI response fields cannot replace type or ID.')
        deadline = time.monotonic() + _timeout(timeout)
        frame = self._frame(dict(result, type='extension_ui_response', id=request_id))
        with self._lock:
            self._check()
        self._write(frame, _Pending('extension_ui_response'), deadline)

    def next_event(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + _timeout(timeout)
        consumer = threading.current_thread()
        with self._changed:
            self._check()
            if self._event_consumer is None:
                self._event_consumer = consumer
            elif self._event_consumer is not consumer:
                raise TransportError('event_consumer_changed')
            while True:
                self._check()
                if self._events:
                    event, size = self._events.popleft()
                    self._event_bytes -= size
                    return event
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError('No native Pi event arrived.')
                self._changed.wait(remaining)

    def _read(self, stream):
        if select.select([stream], [], [], 0.05)[0]:
            return os.read(stream.fileno(), 65536)
        return None

    def _stdout_loop(self):
        buffer = bytearray()
        searched = 0
        try:
            while not self._stop_io.is_set():
                chunk = self._read(self._process.stdout)
                if chunk is None:
                    continue
                if not chunk:
                    if not self._closing:
                        self._fail(ProtocolError('truncated_frame') if buffer else TransportError('stdout_closed'))
                    return
                if self._closing:
                    buffer.clear()
                    searched = 0
                    continue
                buffer.extend(chunk)
                while True:
                    # Large native image/history lines arrive over many reads.
                    # Bytes already checked before this read cannot contain LF.
                    index = buffer.find(b'\n', searched)
                    if index < 0:
                        searched = len(buffer)
                        break
                    with memoryview(buffer) as view:
                        line = bytes(view[:index])
                    del buffer[:index + 1]
                    searched = 0
                    if len(line) > self._max_frame_bytes:
                        raise ProtocolError('frame_limit')
                    self._dispatch(line.removesuffix(b'\r'))
                if len(buffer) > self._max_frame_bytes:
                    raise ProtocolError('frame_limit')
        except ProtocolError as error:
            self._fail(error)
        except (OSError, ValueError):
            if not self._closing:
                self._fail(TransportError('stdout_read_failed'))

    def _dispatch(self, line):
        try:
            message = json.loads(line.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                                 parse_constant=_reject_constant)
            _json_value(message)
            if type(message) is not dict or type(message.get('type')) is not str or not message['type']:
                raise ValueError('Native frame must have a type.')
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError('invalid_json_frame') from None
        with self._changed:
            if self._closing:
                return
            if message['type'] == 'response':
                if (type(message.get('id')) is not str or not message['id']
                        or type(message.get('success')) is not bool
                        or type(message.get('command')) is not str or not message['command']):
                    raise ProtocolError('invalid_response')
                pending = self._pending.get(message['id'])
                if pending is None or pending.done.is_set():
                    self._ignored_responses = min(2**63 - 1, self._ignored_responses + 1)
                    return
                if message['command'] != pending.command:
                    raise ProtocolError('response_command_mismatch')
                if pending.written != pending.frame_size:
                    raise ProtocolError('response_before_submission')
                pending.response = message
                pending.done.set()
            else:
                if len(self._events) >= self._max_events or self._event_bytes + len(line) > self._max_event_bytes:
                    raise ProtocolError('event_limit')
                self._events.append((message, len(line)))
                self._event_bytes += len(line)
                self._events_received += 1
                self._changed.notify_all()

    def _stderr_loop(self):
        try:
            while not self._stop_io.is_set():
                chunk = self._read(self._process.stderr)
                if chunk is None:
                    continue
                if not chunk:
                    return
                with self._lock:
                    total = self._stderr_bytes + len(chunk)
                    self._stderr_saturated |= total > 1024 * 1024
                    self._stderr_bytes = min(1024 * 1024, total)
        except (OSError, ValueError):
            if not self._closing:
                self._fail(TransportError('stderr_read_failed'))

    def _fail(self, error, *, graceful=False):
        with self._changed:
            if self._failure is not None:
                return
            self._failure, self._closing = error, True
            for pending in self._pending.values():
                if not pending.done.is_set():
                    pending.error = type(error)(error.reason, result_unknown=bool(pending.written))
                    pending.done.set()
            self._pending.clear()
            self._events.clear()
            self._event_bytes = 0
            self._changed.notify_all()
            if not self._cleanup_started:
                self._cleanup_started = True
                threading.Thread(target=self._cleanup, args=(graceful,), name='pi-rpc-cleanup', daemon=True).start()

    def _signal_owned(self, sig):
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == 'posix':
            try:
                if self._owned_pgid != process.pid or os.getpgid(process.pid) != self._owned_pgid:
                    raise OSError('Cannot confirm owned process group.')
                os.killpg(self._owned_pgid, sig)
            except ProcessLookupError:
                return
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
        self._shutdown_method = 'term' if sig == signal.SIGTERM else 'kill'

    def _cleanup(self, graceful):
        error = None
        process = self._process
        try:
            if process is not None:
                if graceful and self._write_lock.acquire(timeout=0.25):
                    try:
                        process.stdin.close()
                        self._shutdown_method = 'stdin_eof'
                    except (OSError, ValueError):
                        pass
                    finally:
                        self._write_lock.release()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    try:
                        self._signal_owned(signal.SIGTERM)
                    except OSError:
                        # The exact Popen child handle is still ours even if its
                        # group cannot be authenticated. Do not signal that group.
                        error = 'owned_group_unconfirmed'
                        process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        try:
                            self._signal_owned(signal.SIGKILL)
                        except OSError:
                            error = error or 'owned_group_unconfirmed'
                            process.kill()
                        process.wait(timeout=1)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            error = error or 'process_shutdown_failed'
        finally:
            self._stop_io.set()
            for thread in self._threads:
                thread.join(timeout=0.5)
                if thread.is_alive():
                    error = error or 'io_shutdown_failed'
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        error = error or 'pipe_shutdown_failed'
                if process.poll() is None:
                    error = error or 'process_shutdown_failed'
            self._cleanup_error = error
            self._cleanup_done.set()

    def close(self):
        """Close stdin, then stop only the authenticated owned process/group."""
        self._fail(TransportError('closed'), graceful=True)
        if not self._cleanup_done.wait(timeout=3):
            raise TransportError('cleanup_timeout')
        if self._cleanup_error or any(t.is_alive() for t in self._threads):
            raise TransportError(self._cleanup_error or 'io_shutdown_failed')

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
