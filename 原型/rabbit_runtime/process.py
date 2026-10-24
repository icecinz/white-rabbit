"""Bounded JSONL transport for independent local Agent adapters.

Adapters own configuration, private directories and message semantics. This
module owns only pipes, deadlines, cancellation and process-group cleanup.
"""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


__all__ = ('ProcessError', 'invoke', 'observe_process_group')


class ProcessError(RuntimeError):
    def __init__(self, code):
        self.safe_diagnostic = {'code': code, 'phase': 'transport'}
        super().__init__({'timeout': 'AI 连接响应超时。',
            'invalid_response': 'AI 连接未返回有效完整回执。',
            'cleanup_unconfirmed': '请求已中止，但后台进程终止尚未确认。',
            'process_failed': 'AI 连接进程未正常完成。'}.get(code, 'AI 连接未完成。'))


def observe_process_group(group):
    try:
        value = subprocess.run(['/bin/ps', '-g', str(group), '-o', 'stat='],
            capture_output=True, text=True, timeout=1, env={'LC_ALL': 'C'})
        states = value.stdout.split()
        if (value.stderr.strip() or value.returncode not in (0, 1) or len(states) > 4096
                or any(re.fullmatch(r'[A-Za-z0-9<>+]{1,20}', row) is None for row in states)):
            return None
        return any(not row.startswith('Z') for row in states)
    except (OSError, subprocess.SubprocessError):
        return None


def _terminate(process):
    if process.poll() is not None and observe_process_group(process.pid) is False:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise ProcessError('cleanup_unconfirmed') from None
            return
        except PermissionError:
            if observe_process_group(process.pid) is not False:
                raise ProcessError('cleanup_unconfirmed') from None
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise ProcessError('cleanup_unconfirmed') from None
        if process.poll() is not None and observe_process_group(process.pid) is False:
            return
    raise ProcessError('cleanup_unconfirmed')


def invoke(command, environment, payload, consumer, *, cancel=None, timeout,
           cwd=None, cancel_payload=None, cancel_grace=2, max_input=1_004_096,
           max_line=2_004_096, max_output=8_000_000):
    """Invoke one private worker; a zero exit is only transport completion.

With cancel_payload, stdin stays open after the initial frame. Cancellation
has a short cooperative deadline before mandatory process-group cleanup.
No events after cancellation are delivered to the adapter consumer.
"""
    payload = b'' if payload is None else payload
    if (not isinstance(payload, bytes) or len(payload) > max_input
            or cancel_payload is not None and (not isinstance(cancel_payload, bytes)
                or not 0 < len(cancel_payload) <= 4096)
            or not 0 < timeout <= 3600 or not 0 <= cancel_grace <= 10
            or not all(type(value) is int and 0 < value <= 64_000_000
                       for value in (max_input, max_line, max_output))
            or not isinstance(cwd, (str, Path)) or not Path(cwd).is_dir()):
        raise ValueError('AI 进程请求或范围无效。')
    if cancel is not None and cancel.is_set():
        raise InterruptedError('已停止这次回答。')
    try:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except (OSError, ValueError):
        raise ProcessError('process_failed') from None
    selector = selectors.DefaultSelector()
    deadline, cancel_deadline = time.monotonic() + timeout, None
    total, buffer, offset, reading, writing = 0, b'', 0, True, bool(payload)
    initial_complete = not payload
    pending = payload

    def consume(line):
        if len(line) > max_line:
            raise ProcessError('invalid_response')
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            raise ProcessError('invalid_response') from None
        if not isinstance(value, dict):
            raise ProcessError('invalid_response')
        if cancel_deadline is None and not (cancel is not None and cancel.is_set()):
            consumer(value)

    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        if writing:
            selector.register(process.stdin, selectors.EVENT_WRITE)
        elif cancel_payload is None:
            process.stdin.close()
        while reading:
            now = time.monotonic()
            if cancel is not None and cancel.is_set() and cancel_deadline is None:
                if cancel_payload is None or not initial_complete or process.stdin.closed:
                    raise InterruptedError('已停止这次回答。')
                cancel_deadline = now + cancel_grace
                pending, offset, writing = cancel_payload, 0, True
                selector.register(process.stdin, selectors.EVENT_WRITE)
            if cancel_deadline is not None and now >= cancel_deadline:
                raise InterruptedError('已停止这次回答。')
            if now >= deadline:
                if cancel_deadline is not None:
                    raise InterruptedError('已停止这次回答。')
                raise ProcessError('timeout')
            for key, _ in selector.select(timeout=.05):
                if key.fileobj is process.stdin:
                    try:
                        offset += os.write(key.fd, pending[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                        writing = False
                        continue
                    if offset == len(pending):
                        selector.unregister(process.stdin)
                        writing = False
                        initial_complete = True
                        if cancel_payload is None or cancel_deadline is not None:
                            process.stdin.close()
                else:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(process.stdout)
                        reading = False
                        if buffer:
                            consume(buffer)
                        break
                    total += len(chunk)
                    buffer += chunk
                    if total > max_output:
                        raise ProcessError('invalid_response')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        consume(line)
                    if len(buffer) > max_line:
                        raise ProcessError('invalid_response')
        if cancel_deadline is not None or cancel is not None and cancel.is_set():
            raise InterruptedError('已停止这次回答。')
        if not initial_complete:
            raise ProcessError('invalid_response')
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('已停止这次回答。')
            if time.monotonic() >= deadline:
                raise ProcessError('timeout')
            time.sleep(.05)
        code = process.returncode
        if code:
            raise ProcessError('process_failed')
    finally:
        try:
            _terminate(process)
        finally:
            selector.close()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
