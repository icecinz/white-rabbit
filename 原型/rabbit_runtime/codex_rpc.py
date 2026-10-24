"""Bounded JSONL process transport, originally used by the Codex app-server.

This module owns only its child process and the protocol connection. It does
not initialize an Agent, choose a model, resume a thread, or approve a request.
No payload or stderr text is included in diagnostics or exception messages.
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


class CodexRpcError(Exception):
    """Base exception with no private protocol text in its string form."""


class TransportError(CodexRpcError):
    def __init__(self, reason="transport_failed", *, result_unknown=False):
        super().__init__("Native RPC transport failed (" + reason + ").")
        self.reason = reason
        self.result_unknown = result_unknown


class ProtocolError(TransportError):
    """An unreadable or contradictory frame invalidates the connection."""


class RemoteError(CodexRpcError):
    def __init__(self, code):
        super().__init__("Native RPC request was rejected by the server.")
        self.code = code
        self.result_unknown = False


class SubmittedTimeout(TransportError):
    def __init__(self):
        super().__init__("submitted_timeout", result_unknown=True)


class KnownUnsentTimeout(TransportError):
    def __init__(self):
        super().__init__("not_sent_timeout", result_unknown=False)


def _id_key(value):
    if type(value) is int:
        return (int, value)
    if type(value) is str:
        return (str, value)
    raise ValueError("RPC IDs must be integers or strings; booleans are invalid.")


def validate_json_value(value):
    """Validate a finite JSON tree without copying it, returning None."""
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8", "strict")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            validate_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings.")
            key.encode("utf-8", "strict")
            validate_json_value(item)
        return
    raise ValueError("Only finite, well-formed JSON values are accepted.")


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number.")
    return result


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON constant.")


def validate_timeout(value):
    """Validate a finite positive timeout and return its existing float representation."""
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be a positive finite number.")
    return float(value)


@dataclass
class _Write:
    frame: bytes
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    written: int = 0
    cancelled: bool = False
    error: Exception | None = None


@dataclass
class _Pending:
    write: _Write
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Exception | None = None


_UNSET = object()


class CodexRpc:
    def __init__(self, command, cwd=None, env=None, max_frame_bytes=8 * 1024 * 1024,
                 max_events=1024):
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("Command must be a nonempty argument list, without a shell.")
        self._command = [os.fspath(arg) for arg in command]
        if any(type(arg) is not str or not arg or "\0" in arg for arg in self._command):
            raise ValueError("Command arguments must be nonempty strings.")
        for value in (max_frame_bytes, max_events):
            if type(value) is not int or value <= 0:
                raise ValueError("Transport limits must be positive integers.")
        self._cwd = None if cwd is None else os.fspath(cwd)
        if env is not None and (not isinstance(env, dict) or any(
                type(key) is not str or type(value) is not str
                or "\0" in key or "\0" in value for key, value in env.items())):
            raise ValueError("Environment must contain string keys and values.")
        self._env = None if env is None else dict(env)
        self._max_frame_bytes = max_frame_bytes
        self._max_events = max_events
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._stop_io = threading.Event()
        self._cleanup_done = threading.Event()
        self._process = None
        self._owned_pgid = None
        self._owned_group_cleanup = "unverified"
        self._started = False
        self._failure = None
        self._next_id = 0
        self._pending = {}
        self._server_requests = {}
        self._events = deque()
        self._writes = deque()
        self._active_write = None
        self._threads = []
        self._cleanup_started = False
        self._cleanup_error = None
        self._graceful_shutdown = False
        self._shutdown_method = "none"
        self._stderr_bytes = 0
        self._stderr_saturated = False
        self._ignored_responses = 0

    @property
    def started(self):
        with self._lock:
            return self._started

    @property
    def running(self):
        with self._lock:
            return (self._process is not None and self._failure is None
                    and self._process.poll() is None)

    @property
    def stats(self):
        with self._lock:
            return {"pending_requests": len(self._pending),
                    "pending_server_requests": len(self._server_requests),
                    "queued_events": len(self._events),
                    "queued_writes": len(self._writes),
                    "stderr_bytes": self._stderr_bytes,
                    "stderr_saturated": self._stderr_saturated,
                    "ignored_responses": self._ignored_responses,
                    "owned_group_cleanup": self._owned_group_cleanup,
                    "shutdown_method": self._shutdown_method,
                    "owned_group_id": self._owned_pgid,
                    "process_exited": self._process is None or self._process.poll() is not None,
                    "io_threads_stopped": not any(thread.is_alive() for thread in self._threads),
                    "live_io_threads": sum(thread.is_alive() for thread in self._threads)}

    def start(self):
        with self._lock:
            self._check_failure()
            if self._started:
                return self
            try:
                self._process = subprocess.Popen(
                    self._command, cwd=self._cwd, env=self._env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0, shell=False,
                    start_new_session=os.name == "posix")
                if os.name == "posix":
                    group = os.getpgid(self._process.pid)
                    if group != self._process.pid:
                        raise OSError("Owned process group could not be established.")
                    self._owned_pgid = group
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    os.set_blocking(stream.fileno(), False)
            except (OSError, ValueError):
                self._fail(TransportError("start_failed"))
                raise TransportError("start_failed") from None
            self._started = True
            for name, target in (("writer", self._write_loop),
                                 ("stdout", self._stdout_loop),
                                 ("stderr", self._stderr_loop)):
                thread = threading.Thread(target=target, name="codex-rpc-" + name, daemon=True)
                try:
                    thread.start()
                except RuntimeError:
                    self._fail(TransportError("start_failed"))
                    raise TransportError("start_failed") from None
                self._threads.append(thread)
        return self

    def _check_failure(self):
        if self._failure is not None:
            raise type(self._failure)(self._failure.reason) from None

    def _check_started(self):
        self._check_failure()
        if not self._started:
            raise TransportError("not_started")

    def _frame(self, value):
        try:
            validate_json_value(value)
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8", "strict")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Invalid JSON payload.") from None
        if len(encoded) > self._max_frame_bytes:
            raise ValueError("RPC frame exceeds the configured limit.")
        return encoded + b"\n"

    def _method(self, method, params, request_id=_UNSET):
        if type(method) is not str or not method or len(method) > 256 or "\0" in method:
            raise ValueError("RPC method must be a bounded nonempty string.")
        message = {"method": method}
        if params is not None:
            message["params"] = params
        if request_id is not _UNSET:
            message["id"] = request_id
        return message

    def _enqueue(self, job):
        self._check_started()
        if len(self._writes) >= self._max_events:
            raise TransportError("write_queue_full")
        self._writes.append(job)
        self._changed.notify_all()

    def request(self, method, params=None, timeout=10.0):
        deadline = time.monotonic() + validate_timeout(timeout)
        with self._lock:
            self._check_started()
            if len(self._pending) >= self._max_events:
                raise TransportError("pending_request_limit")
            self._next_id += 1
            request_id = self._next_id
            job = _Write(self._frame(self._method(method, params, request_id)), deadline)
            pending = _Pending(job)
            key = _id_key(request_id)
            self._pending[key] = pending
            try:
                self._enqueue(job)
            except Exception:
                self._pending.pop(key, None)
                raise
        pending.done.wait(max(0, deadline - time.monotonic()))
        with self._lock:
            if pending.done.is_set():
                if pending.error is not None:
                    raise pending.error from None
                return pending.result
            self._pending.pop(key, None)
            error, abort = self._expire_write(job)
        if abort:
            self._fail(TransportError("incomplete_write_timeout"))
        raise error from None

    def notify(self, method, params=None, timeout=10.0):
        self._send(self._method(method, params), timeout)

    def respond(self, request_id, result=_UNSET, error=_UNSET, timeout=10.0):
        key = _id_key(request_id)
        if (result is _UNSET) == (error is _UNSET):
            raise ValueError("Provide exactly one result or error.")
        if error is not _UNSET:
            self._valid_error(error)
        message = {"id": request_id, "result": result} if error is _UNSET else {"id": request_id, "error": error}
        frame = self._frame(message)
        deadline = time.monotonic() + validate_timeout(timeout)
        with self._lock:
            self._check_started()
            if key not in self._server_requests or self._server_requests[key] is not None:
                raise ValueError("The server request is no longer awaiting a response.")
            job = _Write(frame, deadline)
            self._server_requests[key] = job
            try:
                self._enqueue(job)
            except Exception:
                self._server_requests[key] = None
                raise
        try:
            self._wait_write(job)
        finally:
            with self._lock:
                if self._server_requests.get(key) is job:
                    self._server_requests.pop(key, None)

    def forget_request(self, request_id):
        """Invalidate only an explicit, currently unanswered server ID.

        A response already being written cannot be retracted. The caller must
        retain its submitted decision until the native server confirms it.
        """
        key = _id_key(request_id)
        with self._lock:
            if key not in self._server_requests or self._server_requests[key] is not None:
                return False
            self._server_requests.pop(key)
            return True

    def next_event(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + validate_timeout(timeout)
        with self._changed:
            while True:
                self._check_started()
                if self._events:
                    return self._events.popleft()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("No native RPC event arrived within the timeout.")
                self._changed.wait(remaining)

    def _send(self, message, timeout):
        job = _Write(self._frame(message), time.monotonic() + validate_timeout(timeout))
        with self._changed:
            self._enqueue(job)
        self._wait_write(job)

    def _wait_write(self, job):
        job.done.wait(max(0, job.deadline - time.monotonic()))
        with self._lock:
            if job.done.is_set():
                if job.error is not None:
                    raise job.error from None
                return
            error, abort = self._expire_write(job)
        if abort:
            self._fail(TransportError("incomplete_write_timeout"))
        raise error from None

    def _expire_write(self, job):
        job.cancelled = True
        error = SubmittedTimeout() if job.written else KnownUnsentTimeout()
        job.error = error
        job.done.set()
        return error, 0 < job.written < len(job.frame)

    def _wait_ready(self, fd, *, writing=False):
        # POSIX waits wake immediately on pipe activity; the Windows fallback
        # uses nonblocking pipes without assuming select supports pipe handles.
        if os.name == "posix":
            select.select([] if writing else [fd], [fd] if writing else [], [], 0.1)
        else:
            (self._closed if writing else self._stop_io).wait(0.005)

    def _write_loop(self):
        while not self._closed.is_set():
            with self._changed:
                while not self._writes and not self._closed.is_set():
                    self._changed.wait()
                if self._closed.is_set():
                    return
                job = self._writes.popleft()
                self._active_write = job
            try:
                with self._write_lock:
                    fd = self._process.stdin.fileno()
                    while job.written < len(job.frame):
                        with self._lock:
                            if self._closed.is_set() or job.cancelled:
                                break
                            if time.monotonic() >= job.deadline:
                                error, abort = self._expire_write(job)
                                if abort:
                                    self._fail(TransportError("incomplete_write_timeout"))
                                break
                            try:
                                count = os.write(fd, job.frame[job.written:])
                            except BlockingIOError:
                                count = None
                            if count == 0:
                                raise OSError("Pipe write ended.")
                            if count is not None:
                                job.written += count
                        if count is None:
                            self._wait_ready(fd, writing=True)
                with self._lock:
                    if not job.done.is_set() and job.written == len(job.frame):
                        job.done.set()
            except (OSError, ValueError):
                self._fail(TransportError("write_failed"))
                return
            finally:
                with self._lock:
                    if self._active_write is job:
                        self._active_write = None

    def _read_chunk(self, stream):
        fd = stream.fileno()
        while not self._stop_io.is_set():
            try:
                return os.read(fd, 65536)
            except BlockingIOError:
                self._wait_ready(fd)
        return None

    def _stdout_loop(self):
        buffer = bytearray()
        try:
            while not self._stop_io.is_set():
                chunk = self._read_chunk(self._process.stdout)
                if chunk is None:
                    return
                if not chunk:
                    self._fail(ProtocolError("truncated_frame") if buffer else TransportError("eof"))
                    return
                if self._closed.is_set():
                    buffer.clear()
                    continue  # Graceful shutdown drains, without new events.
                buffer.extend(chunk)
                while b"\n" in buffer:
                    if self._closed.is_set():
                        buffer.clear()
                        break
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if len(line) > self._max_frame_bytes:
                        raise ProtocolError("frame_too_large")
                    try:
                        message = json.loads(line.decode("utf-8", "strict"),
                                             object_pairs_hook=_object_pairs,
                                             parse_float=_finite_float,
                                             parse_constant=_reject_constant)
                        validate_json_value(message)
                    except (ValueError, TypeError, UnicodeError, RecursionError):
                        raise ProtocolError("invalid_json") from None
                    self._dispatch(message)
                if len(buffer) > self._max_frame_bytes:
                    raise ProtocolError("frame_too_large")
        except ProtocolError as error:
            self._fail(error)
        except (OSError, ValueError):
            self._fail(TransportError("read_failed"))

    def _stderr_loop(self):
        try:
            while not self._stop_io.is_set():
                chunk = self._read_chunk(self._process.stderr)
                if not chunk:
                    return
                with self._lock:
                    limit = 1024 * 1024
                    total = self._stderr_bytes + len(chunk)
                    self._stderr_saturated |= total > limit
                    self._stderr_bytes = min(limit, total)
        except (OSError, ValueError):
            if not self._closed.is_set():
                self._fail(TransportError("stderr_read_failed"))

    @staticmethod
    def _valid_error(error):
        if (type(error) is not dict or type(error.get("code")) is not int
                or type(error.get("message")) is not str
                or set(error) - {"code", "message", "data"}):
            raise ValueError("RPC error must contain an integer code and a message.")

    def _response_result(self, value):
        """Pure protocol projection under the wire lock, before waking the waiter."""
        return value

    def _remote_error(self, error):
        """Default Codex errors retain their existing code-only contract."""
        return RemoteError(error["code"])

    @staticmethod
    def _valid_event_metadata(message):
        if "emittedAtMs" in message:
            stamp = message["emittedAtMs"]
            if ("method" not in message or type(stamp) is not int
                    or not 0 <= stamp <= (1 << 63) - 1):
                raise ProtocolError("invalid_telemetry")
        if "trace" in message:
            # The installed JSONRPCRequest schema defines nullable trace
            # objects with nullable string traceparent/tracestate fields.
            # This metadata remains opaque and never changes RPC identity.
            trace = message["trace"]
            if "method" not in message or "id" not in message:
                raise ProtocolError("invalid_trace")
            if trace is not None and (type(trace) is not dict
                    or set(trace) - {"traceparent", "tracestate"}
                    or any(value is not None and type(value) is not str
                           for value in trace.values())):
                raise ProtocolError("invalid_trace")

    def _dispatch(self, message):
        if (type(message) is not dict or set(message) - {"jsonrpc", "id", "method", "params", "result", "error", "emittedAtMs", "trace"}
                or "jsonrpc" in message and message["jsonrpc"] != "2.0"):
            raise ProtocolError("invalid_envelope")
        self._valid_event_metadata(message)
        with self._changed:
            if self._closed.is_set():
                return
            if "method" in message:
                if (type(message["method"]) is not str or not message["method"]
                        or "result" in message or "error" in message):
                    raise ProtocolError("invalid_envelope")
                if "id" in message:
                    try:
                        key = _id_key(message["id"])
                    except ValueError:
                        raise ProtocolError("invalid_id") from None
                    if key in self._server_requests:
                        raise ProtocolError("duplicate_server_request")
                    if len(self._server_requests) >= self._max_events:
                        raise ProtocolError("server_request_limit")
                    self._server_requests[key] = None
                if len(self._events) >= self._max_events:
                    raise ProtocolError("event_queue_overflow")
                self._events.append(message)
                self._changed.notify_all()
                return
            if ("id" not in message or "params" in message
                    or ("result" in message) == ("error" in message)):
                raise ProtocolError("invalid_envelope")
            try:
                key = _id_key(message["id"])
                if "error" in message:
                    self._valid_error(message["error"])
            except ValueError:
                raise ProtocolError("invalid_response") from None
            pending = self._pending.get(key)
            if pending is None:
                self._ignored_responses = min(self._ignored_responses + 1, 1024 * 1024)
                return
            if pending.write.written != len(pending.write.frame):
                raise ProtocolError("response_before_submission")
            # Project before removing pending: if a protocol hook rejects the
            # response, _fail must still wake this request as unknown.
            if "error" in message:
                error = self._remote_error(message["error"])
            else:
                result = self._response_result(message["result"])
            self._pending.pop(key)
            if "error" in message:
                pending.error = error
            else:
                pending.result = result
            pending.done.set()

    def _fail(self, error, *, graceful=False):
        with self._changed:
            if self._failure is not None:
                return
            self._failure = error
            self._closed.set()
            partial_write = (self._active_write is not None
                             and 0 < self._active_write.written < len(self._active_write.frame))
            self._graceful_shutdown = graceful and not partial_write
            if not self._graceful_shutdown:
                self._stop_io.set()
            for pending in self._pending.values():
                pending.error = pending.write.error or type(error)(error.reason, result_unknown=bool(pending.write.written))
                pending.done.set()
            self._pending.clear()
            for job in list(self._writes) + ([self._active_write] if self._active_write else []):
                if not job.done.is_set():
                    job.error = type(error)(error.reason, result_unknown=bool(job.written))
                    job.done.set()
            self._writes.clear()
            self._events.clear()
            self._server_requests.clear()
            self._changed.notify_all()
            if not self._cleanup_started:
                self._cleanup_started = True
                threading.Thread(target=self._cleanup, name="codex-rpc-cleanup", daemon=True).start()

    def _cleanup(self):
        cleanup_error = None
        try:
            process = self._process
            if process is not None:
                exited = False
                if self._graceful_shutdown and self._write_lock.acquire(timeout=0.25):
                    try:
                        if process.stdin is not None:
                            process.stdin.close()
                            self._shutdown_method = "stdin_eof"
                    except OSError:
                        cleanup_error = "pipe_shutdown_failed"
                    finally:
                        self._write_lock.release()
                    if cleanup_error is None:
                        try:
                            process.wait(timeout=0.5)
                            exited = True
                        except subprocess.TimeoutExpired:
                            pass
                        except OSError:
                            cleanup_error = "process_shutdown_failed"
                try:
                    if not exited:
                        self._signal_owned(signal.SIGTERM)
                except OSError:
                    cleanup_error = "group_shutdown_failed"
                    # A group cannot be verified: target only the exact child
                    # handle, never the uncertain numeric group.
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except OSError:
                        cleanup_error = "process_shutdown_failed"
                if not exited:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        try:
                            self._signal_owned(signal.SIGKILL)
                            process.wait(timeout=1.0)
                        except (OSError, subprocess.TimeoutExpired):
                            cleanup_error = "process_shutdown_failed"
                    except OSError:
                        cleanup_error = "process_shutdown_failed"
                self._stop_io.set()
                for thread in self._threads:
                    if thread is not threading.current_thread():
                        thread.join(timeout=0.5)
                    if thread.is_alive():
                        cleanup_error = cleanup_error or "reader_shutdown_failed"
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            cleanup_error = cleanup_error or "pipe_shutdown_failed"
        finally:
            self._stop_io.set()
            with self._lock:
                self._cleanup_error = cleanup_error
            self._cleanup_done.set()

    def _signal_owned(self, sig):
        process = self._process
        if process is None:
            return
        if os.name == "posix" and self._owned_pgid == process.pid:
            # Verify the live owner again. After it exits a numeric group can
            # no longer be authenticated by that owner, so do not signal it.
            # Tools that outlive the parent or form other groups are outside
            # the cleanup guarantee and require upper-layer observation.
            try:
                if os.getpgid(process.pid) != self._owned_pgid:
                    raise OSError("Owned process group changed.")
            except ProcessLookupError:
                self._owned_group_cleanup = "unverified"
                return
            try:
                os.killpg(self._owned_pgid, sig)
                self._shutdown_method = "term" if sig == signal.SIGTERM else "kill"
                if sig == signal.SIGKILL:
                    self._owned_group_cleanup = "confirmed"
            except ProcessLookupError:
                self._owned_group_cleanup = "unverified"
        elif process.poll() is None:
            if sig == signal.SIGTERM:
                process.terminate()
                self._shutdown_method = "term"
            else:
                process.kill()
                self._shutdown_method = "kill"

    def close(self):
        self._fail(TransportError("closed"), graceful=True)
        if not self._cleanup_done.wait(timeout=3.0):
            raise TransportError("cleanup_timeout")
        if self._cleanup_error:
            raise TransportError(self._cleanup_error)
        if any(thread.is_alive() for thread in self._threads):
            raise TransportError("reader_shutdown_failed")

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
