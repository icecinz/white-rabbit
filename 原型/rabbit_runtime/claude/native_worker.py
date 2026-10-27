"""Small process boundary around the installed official Claude Python SDK.

One SDK reader owns native events. Fixed startup UUID/cwd, original CLI,
tools/settings/system prompt and explicit one-shot approvals stay native. No
retry, implicit new session, transcript database or Agent loop lives here.
"""
import argparse
import asyncio
from dataclasses import asdict, is_dataclass
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .material_protocol import (MAX_RPC_FRAME_BYTES, user_content,
    ClaudeMaterialProtocolError, canonical_metadata, MAX_RECORD_BYTES)
from .message_view import project_records, original_record, ClaudeMessageViewError
from .history_source import read_sdk_messages, ClaudeHistorySourceError
from .._version import VERSION

MAX_FRAME = MAX_RPC_FRAME_BYTES
MAX_MESSAGE = 1024 * 1024
MAX_MODEL_LENGTH = 192
INIT_FIELDS = {'type', 'subtype', 'session_id', 'cwd', 'model', 'tools', 'permissionMode',
               'claude_code_version', 'mcp_servers', 'slash_commands', 'output_style', 'agents'}


class WorkerError(Exception):
    def __init__(self, code, *, unknown=False):
        self.code, self.unknown = code, unknown


def exact_uuid(value):
    if type(value) is not str:
        raise WorkerError('invalid_uuid')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise WorkerError('invalid_uuid') from None
    if str(parsed) != value:
        raise WorkerError('invalid_uuid')
    return value


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def message_view(message, *, complete=False):
    if not is_dataclass(message) or isinstance(message, type):
        raise WorkerError('sdk_message_invalid', unknown=True)
    name, data = type(message).__name__, asdict(message)
    if name == 'SystemMessage' and data.get('subtype') == 'init':
        original = data.get('data')
        if type(original) is not dict:
            raise WorkerError('sdk_init_invalid', unknown=True)
        data['data'] = {key: value for key, value in original.items() if key in INIT_FIELDS}
        # MCP status can contain arbitrary endpoint/configuration descriptions.
        servers = data['data'].get('mcp_servers')
        if type(servers) is list:
            data['data']['mcp_servers'] = [{key: item[key] for key in ('name', 'status') if key in item}
                for item in servers if type(item) is dict]
    encoded = packed(data)
    if complete:
        try:
            canonical_metadata({'sdk_type': name, 'data': data}, max_bytes=MAX_RECORD_BYTES)
        except ClaudeMaterialProtocolError:
            raise WorkerError('record_capacity') from None
    elif len(encoded) > MAX_MESSAGE:
        preserved = {key: data[key] for key in ('session_id', 'uuid', 'type', 'subtype', 'is_error',
            'num_turns', 'stop_reason', 'terminal_reason') if key in data}
        data = {**preserved, 'truncated': True, 'original_bytes': len(encoded),
                'sha256': hashlib.sha256(encoded).hexdigest()}
    return {'sdk_type': name, 'data': data}


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise WorkerError('duplicate_json_key')
        value[key] = item
    return value


def _constant(_):
    raise WorkerError('nonfinite_json')


class Worker:
    def __init__(self, args, sdk):
        self.args, self.sdk = args, sdk
        self.stop, self.output, self.submit_lock = asyncio.Event(), asyncio.Lock(), asyncio.Lock()
        self.sequence, self.last_request = 0, 0
        self.client = self.native_transport = self.native_process = None
        self.observed, self.active, self.acked = None, None, False
        self.closing, self.problem = False, None
        self.permissions, self.seen_clients, self.handlers = {}, set(), set()
        self.close_request, self.native_file = None, None
        self.info, self.stderr_bytes = None, 0
        self.project_dir = None

    async def write(self, value):
        frame = packed(value) + b'\n'
        if len(frame) > MAX_FRAME:
            self.stop.set()
            raise WorkerError('frame_limit', unknown=True)
        async with self.output:
            try:
                async with asyncio.timeout(5):
                    view = memoryview(frame)
                    loop = asyncio.get_running_loop()
                    while view:
                        try:
                            written = os.write(1, view)
                            view = view[written:]
                        except BlockingIOError:
                            ready = loop.create_future()
                            loop.add_writer(1, lambda: not ready.done() and ready.set_result(None))
                            try:
                                await ready
                            finally:
                                loop.remove_writer(1)
            except (OSError, TimeoutError):
                self.stop.set()
                raise WorkerError('parent_output_closed', unknown=True) from None

    async def emit(self, event):
        self.sequence += 1
        await self.write({'method': 'claude/event', 'params': {'event_seq': self.sequence, 'event': event}})

    async def reply(self, request_id, value=None, error=None):
        envelope = {'ok': error is None, 'event_seq': self.sequence}
        if error is None:
            envelope['value'] = value
        else:
            envelope['error'] = {'code': error.code, 'result_unknown': error.unknown}
        await self.write({'id': request_id, 'result': envelope})

    def require_session(self, params, extra=()):
        if type(params) is not dict or set(params) != {'session_id', *extra}:
            raise WorkerError('invalid_arguments')
        if exact_uuid(params['session_id']) != self.args.session_id:
            raise WorkerError('session_identity')

    def verify_file(self):
        from .session_file import verify_session_file, recheck_session_file
        if self.native_file is None:
            value = verify_session_file(self.args.session_id, self.args.cwd, project_dir=self.project_dir)
        else:
            value = recheck_session_file(self.native_file, self.args.session_id, self.args.cwd,
                                        project_dir=self.project_dir)
        self.native_file = value
        return value

    def file_available(self):
        try:
            return self.verify_file()
        except Exception:
            if self.native_file is not None:
                raise WorkerError('native_file_changed', unknown=bool(self.active)) from None
            return None

    def session_info(self):
        descriptor = self.file_available()
        return {'configured_session_id': self.args.session_id, 'observed_session_id': self.observed,
            'cwd': self.args.cwd, 'running': self.active is not None, 'active_client_id': self.active,
            'identity_verified': self.observed == self.args.session_id,
            'history_available': descriptor is not None, 'native_file': descriptor,
            'pending_permissions': [row['event'] for row in self.permissions.values()],
            'launch_mode': self.args.mode, 'sdk_version': self.args.expected_sdk,
            'cli_version': self.args.expected_cli}

    async def permission(self, name, inputs, context):
        if self.closing:
            return self.sdk.PermissionResultDeny(message='Connection closed.', interrupt=True)
        if len(self.permissions) >= 64:
            self.problem = 'permission_limit'; self.stop.set()
            return self.sdk.PermissionResultDeny(message='Permission queue is full.', interrupt=True)
        token, future = str(uuid.uuid4()), asyncio.get_running_loop().create_future()
        event = {'type': 'permission.request', 'session_id': self.args.session_id, 'client_id': self.active,
            'request_id': token, 'tool_use_id': getattr(context, 'tool_use_id', None),
            'tool_name': name, 'input': inputs,
            **{key: getattr(context, key, None) for key in ('title', 'display_name', 'description', 'blocked_path', 'decision_reason')}}
        if len(packed(event)) > MAX_MESSAGE:
            return self.sdk.PermissionResultDeny(message='The permission request is too large to display.', interrupt=True)
        self.permissions[token] = {'future': future, 'event': event}
        try:
            await self.emit(event)
            decision = await future
            return (self.sdk.PermissionResultAllow() if decision == 'allow_once' else
                    self.sdk.PermissionResultDeny(message='Declined in 小白兔.', interrupt=decision == 'expired'))
        finally:
            row = self.permissions.pop(token, None)
            if row is not None:
                await self.emit({'type': 'permission.resolved', 'session_id': self.args.session_id,
                    'client_id': event['client_id'], 'request_id': token,
                    'state': future.result() if future.done() and not future.cancelled() else 'expired'})

    async def expire_permissions(self):
        for row in list(self.permissions.values()):
            if not row['future'].done():
                row['future'].set_result('expired')
        await asyncio.sleep(0)

    async def observed_identity(self, identity):
        if identity is None:
            return
        if exact_uuid(identity) != self.args.session_id:
            raise WorkerError('native_session_changed', unknown=True)
        if self.observed is None:
            self.observed = identity
            await self.emit({'type': 'session.identity', 'session_id': identity,
                             'client_id': self.active, 'identity_verified': True})
            if self.acked and self.active is not None:
                await self.emit({'type': 'prompt.accepted', 'session_id': identity,
                                 'client_id': self.active, 'native_user_uuid': self.active})

    async def messages(self):
        try:
            async for message in self.client.receive_messages():
                name = type(message).__name__
                parent = getattr(message, 'parent_tool_use_id', None)
                identity = getattr(message, 'session_id', None)
                if name == 'SystemMessage' and getattr(message, 'subtype', None) == 'init':
                    identity = message.data.get('session_id')
                if parent is None:
                    await self.observed_identity(identity)
                origin = getattr(message, 'origin', None)
                human = origin is None or type(origin) is dict and origin.get('kind') == 'human'
                if (name == 'UserMessage' and parent is None and human and self.active is not None
                        and getattr(message, 'uuid', None) == self.active):
                    if not self.acked:
                        self.acked = True
                        if self.observed is not None:
                            await self.emit({'type': 'prompt.accepted', 'session_id': self.args.session_id,
                                'client_id': self.active, 'native_user_uuid': self.active})
                own_result = name == 'ResultMessage' and parent is None and human
                client = None if name == 'ResultMessage' and not own_result else self.active
                await self.emit({'type': 'sdk.message', 'session_id': self.args.session_id,
                    'client_id': client, 'message': message_view(message)})
                if own_result:
                    # This is in-memory single-active correlation, never a
                    # claim of native durable prompt receipts or deduplication.
                    await self.expire_permissions()
                    self.active, self.acked = None, False
            if not self.closing:
                self.problem = 'native_eof'; self.stop.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.problem = 'native_event_failed'; self.stop.set()

    async def _sdk_history(self, offset, limit):
        if self.active is not None or self.permissions:
            raise WorkerError('history_busy')
        before = await asyncio.to_thread(self.verify_file)
        messages, descriptor = await read_sdk_messages(self.sdk,
            session_id=self.args.session_id, cwd=self.args.cwd, project_dir=self.project_dir,
            expected_file=before, offset=offset, limit=limit)
        self.native_file = descriptor
        return messages, descriptor

    async def history(self, params):
        if type(params) is not dict:
            raise WorkerError('invalid_arguments')
        extra = set(params) - {'session_id'} if type(params) is dict else set()
        if extra - {'limit', 'offset'}:
            raise WorkerError('invalid_arguments')
        self.require_session({'session_id': params.get('session_id')})
        limit, offset = params.get('limit', 100), params.get('offset', 0)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 100000:
            raise WorkerError('invalid_arguments')
        try:
            messages, descriptor = await self._sdk_history(offset, limit + 1)
            if type(messages) is not list:
                raise ValueError()
            projected, total = [], 0
            for message in messages[:limit]:
                if getattr(message, 'session_id', None) != self.args.session_id or type(message).__name__ != 'SessionMessage':
                    raise ValueError()
                view = message_view(message)
                size = len(packed(view))
                if projected and total + size > MAX_MESSAGE:
                    break
                projected.append(view); total += size
        except Exception:
            raise WorkerError('history_unverified') from None
        more = len(messages) > len(projected)
        return {'session_id': self.args.session_id, 'cwd': self.args.cwd, 'messages': projected,
                'native_file': descriptor, 'offset': offset, 'has_more': more,
                'next_offset': offset + len(projected) if more else None}

    async def history_page(self, params, *, exact=False):
        """Bounded pages from the official SDK, never a replacement parser."""
        fields = ('offset', 'uuid', 'sha256') if exact else ('offset', 'limit')
        self.require_session(params, fields)
        offset = params['offset']
        limit = 1 if exact else params['limit']
        if (type(offset) is not int or not 0 <= offset <= 100000
                or type(limit) is not int or not 1 <= limit <= 200):
            raise WorkerError('invalid_arguments')
        if self.active is not None or self.permissions:
            raise WorkerError('history_busy')
        try:
            messages, after = await self._sdk_history(offset, limit if exact else limit + 1)
            if type(messages) is not list or len(messages) > limit + (not exact):
                raise WorkerError('history_unverified')
            records = []
            for message in messages[:limit]:
                if (type(message).__name__ != 'SessionMessage'
                        or getattr(message, 'session_id', None) != self.args.session_id):
                    raise WorkerError('history_unverified')
                records.append(message_view(message, complete=True))
            if exact:
                if len(records) != 1:
                    raise WorkerError('history_changed')
                reference = {'offset': offset, 'uuid': params['uuid'], 'sha256': params['sha256'], 'projected': True}
                result = {'record': original_record(records[0], reference), 'reference': reference}
            else:
                result = project_records(records, offset=offset, has_more=len(messages) > limit)
            return {'session_id': self.args.session_id, 'cwd': self.args.cwd, 'native_file': after, **result}
        except WorkerError:
            raise
        except ClaudeMessageViewError as error:
            raise WorkerError('history_' + error.code) from None
        except ClaudeHistorySourceError as error:
            raise WorkerError('history_changed' if error.code == 'source_changed' else 'history_unverified') from None
        except Exception:
            raise WorkerError('history_unverified') from None

    async def request(self, method, params):
        if self.closing:
            raise WorkerError('closing')
        if method == 'session/history':
            async with self.submit_lock:
                return await self.history(params)
        if method in ('session/history.page', 'session/history.record'):
            async with self.submit_lock:
                return await self.history_page(params, exact=method.endswith('.record'))
        if method == 'session.info':
            self.require_session(params)
            return await asyncio.to_thread(self.session_info)
        if method == 'model.options':
            self.require_session(params)
            return {'models': [{key: item[key] for key in ('value', 'displayName', 'description') if key in item}
                               for item in self.info.get('models', []) if type(item) is dict]}
        if method == 'prompt.submit':
            material = type(params) is dict and 'material_input' in params
            explicit_model = type(params) is dict and 'model' in params
            fields = ('client_id', 'message') + (('material_input',) if material else ())
            self.require_session(params, fields + (('model',) if explicit_model else ()))
            client, text = exact_uuid(params['client_id']), params['message']
            if type(text) is not str or not text.strip() or len(text.encode('utf-8')) > 128 * 1024:
                raise WorkerError('invalid_message')
            if explicit_model:
                model = params['model']
                if (type(model) is not str or not 1 <= len(model) <= MAX_MODEL_LENGTH
                        or any(ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                               for char in model)):
                    raise WorkerError('invalid_model')
            try:
                content = user_content(text, params['material_input']) if material else text
            except ClaudeMaterialProtocolError:
                raise WorkerError('invalid_material') from None
            async with self.submit_lock:
                if client in self.seen_clients:
                    raise WorkerError('already_submitted', unknown=True)
                if self.active is not None or self.permissions:
                    raise WorkerError('busy')
                if len(self.seen_clients) >= 100000:
                    raise WorkerError('submission_limit')
                self.seen_clients.add(client)
                if explicit_model:
                    try:
                        await self.client.set_model(model)
                    except Exception:
                        # Only this prompt is known unsent. The native model
                        # may already have changed before its ACK was lost.
                        # Keep the client reserved: neither setter nor query
                        # may be retried under this same sending identity.
                        raise WorkerError('model_apply_unconfirmed') from None
                self.active, self.acked = client, False
                async def one_message():
                    yield {'type': 'user', 'uuid': client, 'session_id': self.args.session_id,
                        'message': {'role': 'user', 'content': content}, 'parent_tool_use_id': None}
                try:
                    await self.client.query(one_message(), session_id=self.args.session_id)
                except Exception:
                    raise WorkerError('query_result_unknown', unknown=True) from None
                return {'submitted': True, 'session_id': self.args.session_id, 'client_id': client}
        if method == 'interrupt':
            self.require_session(params)
            await self.expire_permissions()
            try:
                await self.client.interrupt()
            except Exception:
                raise WorkerError('interrupt_result_unknown', unknown=True) from None
            return {'interrupt_requested': True, 'session_id': self.args.session_id}
        if method == 'permission.respond':
            self.require_session(params, ('request_id', 'decision'))
            token = exact_uuid(params['request_id'])
            if params['decision'] not in ('allow_once', 'deny'):
                raise WorkerError('invalid_permission_decision')
            row = self.permissions.get(token)
            if row is None or row['future'].done():
                raise WorkerError('permission_expired')
            row['future'].set_result(params['decision'])
            return {'responded': True, 'request_id': token, 'session_id': self.args.session_id}
        raise WorkerError('unsupported_method')

    async def handle(self, frame):
        request_id = frame['id']
        try:
            if frame['method'] == 'bridge/ready' and not frame.get('params'):
                value = {'ready': True, 'schema_version': 1, **self.session_info()}
            elif frame['method'] == 'bridge/close' and not frame.get('params'):
                if self.close_request is not None:
                    raise WorkerError('closing')
                self.close_request = request_id
                self.stop.set()
                return
            elif frame['method'] == 'bridge/request':
                params = frame.get('params')
                if type(params) is not dict or set(params) != {'method', 'params'}:
                    raise WorkerError('invalid_arguments')
                value = await self.request(params['method'], params['params'])
            else:
                raise WorkerError('unsupported_method')
            await self.reply(request_id, value)
        except WorkerError as error:
            await self.reply(request_id, error=error)
        except Exception:
            await self.reply(request_id, error=WorkerError('worker_request_failed', unknown=True))

    async def input(self):
        buffer, loop = b'', asyncio.get_running_loop()
        try:
            while not self.closing:
                try:
                    chunk = os.read(0, 65536)
                except BlockingIOError:
                    ready = loop.create_future()
                    loop.add_reader(0, lambda: not ready.done() and ready.set_result(None))
                    try:
                        await ready
                    finally:
                        loop.remove_reader(0)
                    continue
                if not chunk:
                    self.stop.set(); return
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if len(line) > MAX_FRAME or not line:
                        raise WorkerError('input_frame_limit')
                    frame = json.loads(line, object_pairs_hook=_pairs, parse_constant=_constant)
                    if (type(frame) is not dict or set(frame) - {'id', 'method', 'params', 'jsonrpc'}
                            or type(frame.get('id')) is not int or frame['id'] <= self.last_request
                            or type(frame.get('method')) is not str or len(self.handlers) >= 64):
                        raise WorkerError('invalid_parent_frame')
                    self.last_request = frame['id']
                    task = asyncio.create_task(self.handle(frame))
                    self.handlers.add(task); task.add_done_callback(self.handlers.discard)
                if len(buffer) > MAX_FRAME:
                    raise WorkerError('input_frame_limit')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.problem = 'parent_protocol_failed'; self.stop.set()

    async def run(self):
        from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir
        self.project_dir = _get_project_dir(_canonicalize_path(self.args.cwd))
        if self.args.mode == 'resume':
            self.verify_file()  # No native startup or fallback without exact original proof.
        elif (self.project_dir / (self.args.session_id + '.jsonl')).exists() or (self.project_dir / (self.args.session_id + '.jsonl')).is_symlink():
            raise WorkerError('new_session_already_exists')
        def stderr(line):
            self.stderr_bytes = min(1024 * 1024, self.stderr_bytes + len(line.encode('utf-8', 'replace')))
        options = self.sdk.ClaudeAgentOptions(cli_path=self.args.cli, cwd=self.args.cwd,
            system_prompt={'type': 'preset', 'preset': 'claude_code'}, setting_sources=['user', 'project', 'local'],
            can_use_tool=self.permission, stderr=stderr, include_partial_messages=True,
            max_buffer_size=MAX_FRAME,
            extra_args={'replay-user-messages': None},
            **({'resume': self.args.session_id} if self.args.mode == 'resume' else {'session_id': self.args.session_id}))
        self.client = self.sdk.ClaudeSDKClient(options=options)
        readers, disconnected = [], False
        try:
            async with asyncio.timeout(45):
                await self.client.connect()
                self.info = await self.client.get_server_info()
            if type(self.info) is not dict:
                raise WorkerError('sdk_initialize_invalid')
            self.native_transport = getattr(self.client, '_transport', None)
            self.native_process = getattr(self.native_transport, '_process', None)
            if self.args.mode == 'resume':
                self.verify_file()
            readers = [asyncio.create_task(self.input()), asyncio.create_task(self.messages())]
            await self.stop.wait()
        finally:
            self.closing = True
            await self.expire_permissions()
            # Capture even a subprocess that failed during connect before the
            # SDK query object was installed; disconnect alone can miss it.
            transport = self.native_transport or getattr(self.client, '_transport', None)
            process = self.native_process or getattr(transport, '_process', None)
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            try:
                async with asyncio.timeout(25):
                    await self.client.disconnect()
                    if transport is not None and getattr(transport, '_process', None) is not None:
                        await transport.close()
                disconnected = True
            except Exception:
                pass  # The parent still owns and physically checks the group.
            for task in list(self.handlers):
                task.cancel()
            await asyncio.gather(*self.handlers, return_exceptions=True)
            io_closed = (transport is not None and all(getattr(transport, field, None) is None
                for field in ('_process', '_stdin_stream', '_stdout_stream', '_stderr_stream', '_stderr_task')))
            exited = process is not None and getattr(process, 'returncode', None) is not None
            proof = {'closed': disconnected and io_closed and exited, 'sdk_disconnected': disconnected,
                     'native_process_exited': exited, 'native_io_closed': io_closed}
            if self.close_request is not None:
                await self.reply(self.close_request, proof)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cli', 'cwd', 'session-id', 'mode', 'expected-sdk', 'expected-cli', 'expected-runtime'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    exact_uuid(args.session_id)
    if (args.mode not in ('new', 'resume') or not Path(args.cli).is_absolute() or not Path(args.cli).is_file()
            or not Path(args.cwd).is_absolute() or not Path(args.cwd).is_dir()
            or any(re.fullmatch(r'\d+\.\d+\.\d+', value) is None for value in
                   (args.expected_sdk, args.expected_cli, args.expected_runtime))):
        raise WorkerError('invalid_startup')
    return args


def main():
    try:
        args = arguments()
        if args.expected_runtime != VERSION or version('rabbit-runtime') != VERSION:
            raise WorkerError('runtime_version_changed')
        if version('claude-agent-sdk') != args.expected_sdk:
            raise WorkerError('sdk_version_changed')
        probe = subprocess.run([args.cli, '--version'], capture_output=True, timeout=10, check=True)
        if probe.stdout.decode('utf-8').strip() != args.expected_cli + ' (Claude Code)':
            raise WorkerError('cli_version_changed')
        import claude_agent_sdk as sdk
        os.set_blocking(0, False); os.set_blocking(1, False)
        asyncio.run(Worker(args, sdk).run())
        return 0
    except BaseException:
        # Native text can contain prompts, endpoints and account details.
        print('Claude native worker did not complete.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
