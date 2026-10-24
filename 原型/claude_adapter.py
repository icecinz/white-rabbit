"""Disposable Claude text turns using the existing native connection.

Credentials stay in process memory. The app supplies all conversation context;
native tools, slash commands, memories, attachments and transcript persistence
are disabled. `init.skills` lists names independently of available commands in
Claude 2.1.87, so it is bounded metadata rather than executable capability.
"""
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from model_catalog import entry, fingerprint, identifier, image_capability, valid_model
from profile_capabilities import model_effort_capability, validate_profile_effort


PROVIDER = 'Claude configured connection'
RUN_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 12
MAX_CONFIG_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 1_000_000
MAX_LINE_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 8_000_000
MAX_TEXT_BYTES = 2_000_000
MAX_AUTH_BYTES = 65536
ISOLATION_SETTINGS = {'autoMemoryEnabled': False, 'disableAllHooks': True,
                      'claudeMdExcludes': ['**']}
CONNECTION_ENV = frozenset(('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
    'CLAUDE_CODE_EFFORT_LEVEL', 'MAX_THINKING_TOKENS'))
RETAINED_ENV = frozenset(('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'))
ALIAS = re.compile(r'^(?:opus|sonnet|haiku)(?:\[1m\])?$')
_probe_cache = None
_probe_lock = threading.Lock()


class ClaudeAdapterError(RuntimeError):
    def __init__(self, code, *, phase='response', events=0):
        self.safe_diagnostic = {'code': code, 'phase': phase, 'event_count': events}
        super().__init__({'timeout': 'Claude 响应超时，请稍后重试。',
            'isolation_rejected': 'Claude 的文字隔离检查未通过。',
            'configuration_changed': 'Claude 的本机连接配置已变化，请刷新后重新发送。',
            'cleanup_unconfirmed': 'Claude 请求已中止，但后台进程终止尚未确认。',
            'invalid_response': 'Claude 未返回完整的文字回答。',
            'process_failed': 'Claude 请求未完成，请检查本机连接。'}.get(code,
            'Claude 本机连接检查未完成。'))


def _home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def executable():
    for candidate in (os.environ.get('ONE_AI_CLAUDE'), shutil.which('claude'),
                      str(Path.home() / '.local/bin/claude')):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _bounded_settings(path):
    """Read one ordinary settings file through its checked, non-following fd."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}, None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONFIG_BYTES:
            raise ValueError('Claude 本机配置文件无效。')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(fd)
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        current = os.stat(path, follow_symlinks=False)
        if (len(data) > MAX_CONFIG_BYTES or stamp(before) != stamp(after)
                or stamp(after) != stamp(current) or not stat.S_ISREG(current.st_mode)):
            raise ValueError('Claude 本机配置读取未完成。')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('Claude 本机配置格式无效。')
        return value, stamp(after)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('Claude 本机配置格式无效。') from None
    finally:
        os.close(fd)


def _settings():
    binary = executable()
    if not binary:
        raise ValueError('没有找到 Claude Code CLI。')
    raw, stamp = _bounded_settings(_home() / 'settings.json')
    configured = raw.get('env', {})
    if not isinstance(configured, dict):
        raise ValueError('Claude 本机连接配置无效。')
    for key in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        value = configured.get(key, os.environ.get(key, ''))
        if value is True or isinstance(value, str) and value.strip().lower() in ('1', 'true', 'yes', 'on'):
            raise ValueError('Claude 当前文字连接暂未接入该本机提供商。')
    if raw.get('modelOverrides'):
        raise ValueError('Claude 当前文字连接暂未接入自定义模型覆盖。')
    connection = {key: value for key, value in configured.items() if key in CONNECTION_ENV}
    if any(not isinstance(value, str) or len(value) > 65536 or '\x00' in value
           for value in connection.values()):
        raise ValueError('Claude 本机连接配置无效。')
    if not any(connection.get(key) for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                                              'CLAUDE_CODE_OAUTH_TOKEN')):
        raise ValueError('Claude 当前文字连接需要本机设置中的可复用模型凭据。')
    defaults = {}
    if 'effortLevel' in raw:
        if raw['effortLevel'] not in ('low', 'medium', 'high'):
            raise ValueError('Claude 本机默认强度配置无效。')
        defaults['effortLevel'] = raw['effortLevel']
    if 'alwaysThinkingEnabled' in raw:
        if type(raw['alwaysThinkingEnabled']) is not bool:
            raise ValueError('Claude 本机默认思考配置无效。')
        defaults['alwaysThinkingEnabled'] = raw['alwaysThinkingEnabled']
    effort = connection.get('CLAUDE_CODE_EFFORT_LEVEL')
    if effort is not None and effort not in ('low', 'medium', 'high', 'max', 'auto', 'unset'):
        raise ValueError('Claude 本机默认强度配置无效。')
    thinking = connection.get('MAX_THINKING_TOKENS')
    if thinking is not None and re.fullmatch(r'-?\d{1,9}', thinking) is None:
        raise ValueError('Claude 本机默认思考配置无效。')
    model = connection.get('ANTHROPIC_MODEL') or raw.get('model') or 'sonnet'
    if not valid_model(model):
        raise ValueError('Claude 本机默认模型配置无效。')
    endpoint = connection.get('ANTHROPIC_BASE_URL') or ''
    parsed = urlsplit(endpoint)
    if endpoint and (parsed.scheme not in ('http', 'https') or not parsed.hostname
                     or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Claude 本机连接地址无效。')
    binary_stat = Path(binary).stat()
    public = {'default_model': model, 'configured_provider': PROVIDER,
              'base_url': endpoint, 'binary': binary,
              'reasoning_effort': (None if effort in ('auto', 'unset') else effort) if effort is not None
                                  else defaults.get('effortLevel'),
              'source': '本机默认模型；' + ('沿用 settings.env 的配置强度，实际由 CLI 和模型决定' if effort is not None
                else '沿用本机配置强度，实际由 CLI 和模型决定' if defaults else '本机未配置强度，跟随 CLI 默认')}
    # Managed hooks take precedence over flag disableAllHooks in this release.
    # This text bridge does not change or silently run enterprise controls.
    if os.path.lexists('/Library/Application Support/ClaudeCode'):
        raise ValueError('Claude 当前文字连接尚未接入受管理的安装。')
    public['route_fingerprint'] = fingerprint(dict(public, config_stamp=stamp,
        binary_stamp=(binary_stat.st_dev, binary_stat.st_ino, binary_stat.st_size, binary_stat.st_mtime_ns)))
    return public, connection, defaults


def _environment(connection, temporary_home):
    result = {key: value for key, value in os.environ.items() if key in RETAINED_ENV}
    result.update(connection)
    result.update(HOME=temporary_home, CLAUDE_CONFIG_DIR=str(Path(temporary_home) / '.claude'))
    result.update(CLAUDE_CODE_DISABLE_CLAUDE_MDS='1', CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',
        CLAUDE_CODE_DISABLE_ATTACHMENTS='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
        CLAUDE_CODE_SKIP_PROMPT_HISTORY='1', DISABLE_AUTOUPDATER='1')
    return result


def _live_group(group):
    try:
        check = subprocess.run(['/bin/ps', '-g', str(group), '-o', 'stat='],
            capture_output=True, text=True, timeout=1, env={'LC_ALL': 'C'})
        states = check.stdout.split()
        if (check.stderr.strip() or check.returncode not in (0, 1) or len(states) > 4096
                or any(re.fullmatch(r'[A-Za-z0-9<>+]{1,20}', state) is None for state in states)):
            return None
        return any(not state.startswith('Z') for state in states)
    except (OSError, subprocess.SubprocessError):
        return None


def _terminate(process):
    # A leader can exit while descendants still own stdout. Darwin may return
    # EPERM for a group containing only zombies; prove that case before ignoring.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            process.wait(timeout=1)
            return
        except PermissionError:
            if _live_group(process.pid) is not False:
                raise ClaudeAdapterError('cleanup_unconfirmed') from None
            process.wait(timeout=1)
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise ClaudeAdapterError('cleanup_unconfirmed') from None


def _invoke(command, environment, payload, consumer, *, cancel=None, timeout, json_document=False):
    """Pump both Unix pipes without blocking cancellation on a stalled stdin."""
    with tempfile.TemporaryDirectory(prefix='rabbit-claude-', dir='/private/tmp') as name:
        Path(name).chmod(0o700)
        process = subprocess.Popen(command, cwd=name, env=_environment(environment, name), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        payload, offset, total, buffer, reading = payload or b'', 0, 0, b'', True
        if payload:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        else:
            process.stdin.close()
        def consume(line):
            if len(line) > (MAX_AUTH_BYTES if json_document else MAX_LINE_BYTES):
                raise ClaudeAdapterError('invalid_response')
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                raise ClaudeAdapterError('invalid_response') from None
            if not isinstance(event, dict):
                raise ClaudeAdapterError('invalid_response')
            consumer(event)
            if cancel is not None and cancel.is_set():
                raise InterruptedError('已停止这次回答。')
        deadline = time.monotonic() + timeout
        try:
            while reading:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('已停止这次回答。')
                if time.monotonic() >= deadline:
                    raise ClaudeAdapterError('timeout')
                for key, _ in selector.select(timeout=0.05):
                    if key.fileobj is process.stdin:
                        try:
                            offset += os.write(key.fd, payload[offset:offset + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        if offset == len(payload):
                            selector.unregister(process.stdin)
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
                        if total > (MAX_AUTH_BYTES if json_document else MAX_OUTPUT_BYTES):
                            raise ClaudeAdapterError('invalid_response')
                        if not json_document:
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                consume(line)
                        if len(buffer) > (MAX_AUTH_BYTES if json_document else MAX_LINE_BYTES):
                            raise ClaudeAdapterError('invalid_response')
            if offset != len(payload):
                raise ClaudeAdapterError('invalid_response')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClaudeAdapterError('timeout')
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise ClaudeAdapterError('timeout') from None
            if code:
                raise ClaudeAdapterError('process_failed')
        finally:
            try:
                _terminate(process)
            finally:
                selector.close()
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()


def available():
    global _probe_cache
    try:
        settings, connection, _ = _settings()
        with _probe_lock:
            if (_probe_cache and _probe_cache[0] == settings['route_fingerprint']
                    and time.monotonic() - _probe_cache[1] < 15):
                return dict(_probe_cache[2])
            events = []
            _invoke([settings['binary'], '--setting-sources', '', 'auth', 'status', '--json'],
                connection, None, events.append, timeout=PROBE_TIMEOUT_SECONDS, json_document=True)
            ready = len(events) == 1 and events[0].get('loggedIn') is True
            state = {'available': ready, 'reason': '' if ready else 'Claude 需要先配置本机登录或模型连接。',
                     'model': settings['default_model'], 'provider': PROVIDER}
            _probe_cache = (settings['route_fingerprint'], time.monotonic(), state)
            return dict(state)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {'available': False, 'reason': 'Claude 本机连接检查未完成。'}


def capture_route(model=None, *, reasoning_effort=None):
    validate_profile_effort(model_effort_capability('claude', {}), reasoning_effort)
    state = available()
    if not state['available']:
        raise ValueError(state['reason'])
    settings, _, _ = _settings()
    selected = settings['default_model'] if model is None else model
    if selected not in (settings['default_model'], 'sonnet', 'opus', 'haiku'):
        raise ValueError('Claude 模型选择无效，请刷新目录。')
    return dict(settings, backend='claude', provider=PROVIDER, model=selected,
        model_id=identifier('claude', selected), effort_selection='agent_default',
        source=settings['source'] if selected == settings['default_model'] else
               'Claude CLI 模型别名；沿用配置强度，实际由 CLI 和模型决定',
        **image_capability('unsupported', 'Claude 当前连接仅接入文字'),
        **model_effort_capability('claude', {}))


def validate_route(route):
    try:
        settings, _, _ = _settings()
    except (OSError, ValueError):
        raise ClaudeAdapterError('configuration_changed', phase='startup') from None
    if (not isinstance(route, dict) or route.get('backend') != 'claude'
            or route.get('provider') != PROVIDER or not valid_model(route.get('model'))
            or route.get('model') not in (settings['default_model'], 'sonnet', 'opus', 'haiku')
            or route.get('model_id') != identifier('claude', route['model'])
            or route.get('effort_selection', 'agent_default') != 'agent_default'
            or route.get('reasoning_effort') != settings['reasoning_effort']
            or any(route.get(field) != settings[field] for field in
                   ('route_fingerprint', 'base_url', 'configured_provider', 'binary', 'default_model'))):
        raise ClaudeAdapterError('configuration_changed', phase='startup')
    return dict(route)


def model_catalog():
    state = available()
    try:
        settings, _, _ = _settings()
    except (OSError, ValueError):
        return []
    result = []
    for model in dict.fromkeys((settings['default_model'], 'sonnet', 'opus', 'haiku')):
        route = dict(settings, **image_capability('unsupported', 'Claude 当前连接仅接入文字'))
        result.append(entry('claude', model, state, source=settings['source'] if model == settings['default_model']
            else 'Claude CLI 模型别名', is_default=model == settings['default_model'], route=route,
            detail='沿用本机连接；别名是请求选择，实际模型以服务返回为准。连接检查不代表推理已验收。',
            image_input='unsupported', image_input_source='Claude 当前连接仅接入文字'))
    return result


class _Stream:
    def __init__(self, callback):
        self.callback, self.count = callback, 0
        self.initialized, self.result, self.text, self.actual = False, None, '', None
        self.blocks, self.parts, self.last_index, self.partial = {}, [], 0, False
        self.message_id, self.seen_uuids = None, set()

    def accept(self, event):
        self.count += 1
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            skills = event.get('skills', [])
            if (self.initialized or any(event.get(field) != [] for field in
                    ('tools', 'mcp_servers', 'plugins', 'slash_commands'))
                    or not isinstance(skills, list) or len(skills) > 256
                    or any(not isinstance(name, str) or not 0 < len(name) <= 256
                           or any(ord(char) < 32 for char in name) for name in skills)):
                raise ClaudeAdapterError('isolation_rejected', phase='startup', events=self.count)
            self.initialized = True
            return
        if not self.initialized or self.result is not None:
            raise ClaudeAdapterError('invalid_response', events=self.count)
        if kind == 'stream_event':
            value = event.get('event')
            if not isinstance(value, dict):
                raise ClaudeAdapterError('invalid_response', events=self.count)
            block = value.get('content_block', {})
            delta = value.get('delta', {})
            if (not isinstance(block, dict) or not isinstance(delta, dict)
                    or block.get('type') in ('tool_use', 'server_tool_use', 'mcp_tool_use')
                    or delta.get('type') == 'input_json_delta'):
                raise ClaudeAdapterError('isolation_rejected', events=self.count)
            self._stop_reason(delta.get('stop_reason'))
            if value.get('type') == 'message_start':
                message = value.get('message', {})
                if not isinstance(message, dict):
                    raise ClaudeAdapterError('invalid_response', events=self.count)
                self.message_id = message.get('id')
                self.blocks, self.parts, self.last_index, self.partial = {}, [], 0, False
                self.actual = None
            index = value.get('index', self.last_index)
            if type(index) is not int or not 0 <= index < 4096:
                raise ClaudeAdapterError('invalid_response', events=self.count)
            if value.get('type') == 'content_block_start' and block.get('type') == 'text':
                if not isinstance(block.get('text'), str):
                    raise ClaudeAdapterError('invalid_response', events=self.count)
                self.blocks[index], self.last_index, self.partial = block['text'], index, True
                self._text(''.join(self.blocks[key] for key in sorted(self.blocks)))
            if value.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                if not isinstance(delta.get('text'), str):
                    raise ClaudeAdapterError('invalid_response', events=self.count)
                self.blocks[index] = self.blocks.get(index, '') + delta['text']
                self.last_index, self.partial = index, True
                self._text(''.join(self.blocks[key] for key in sorted(self.blocks)))
        elif kind == 'assistant':
            message = event.get('message')
            if (event.get('parent_tool_use_id') is not None or event.get('error')
                    or not isinstance(message, dict) or message.get('role') != 'assistant'
                    or not isinstance(message.get('content'), list)):
                raise ClaudeAdapterError('invalid_response', events=self.count)
            blocks = message['content']
            if any(not isinstance(block, dict) or block.get('type') not in
                   ('text', 'thinking', 'redacted_thinking') for block in blocks):
                raise ClaudeAdapterError('isolation_rejected', events=self.count)
            if any(not isinstance(block.get('text'), str) for block in blocks if block['type'] == 'text'):
                raise ClaudeAdapterError('invalid_response', events=self.count)
            self._stop_reason(message.get('stop_reason'))
            identity, uuid = message.get('id'), event.get('uuid')
            if any(value is not None and (not isinstance(value, str) or not 0 < len(value) <= 256)
                   for value in (identity, uuid)):
                raise ClaudeAdapterError('invalid_response', events=self.count)
            if identity is not None and identity != self.message_id:
                self.blocks, self.parts, self.last_index, self.partial = {}, [], 0, False
                self.message_id = identity
                self.actual = None
            text_blocks = [block['text'] for block in blocks if block['type'] == 'text']
            if text_blocks:
                if self.partial:
                    if len(text_blocks) == 1:
                        self.blocks[self.last_index] = text_blocks[0]
                    else:
                        self.blocks = dict(enumerate(text_blocks))
                    self._text(''.join(self.blocks[key] for key in sorted(self.blocks)))
                else:
                    if uuid is None or uuid not in self.seen_uuids:
                        self.parts = list(text_blocks) if len(text_blocks) > 1 else self.parts + text_blocks
                        if isinstance(uuid, str):
                            self.seen_uuids.add(uuid)
                        self._text(''.join(self.parts))
            model = message.get('model')
            if valid_model(model) and not ALIAS.fullmatch(model):
                self.actual = model
        elif kind == 'result':
            self._stop_reason(event.get('stop_reason'))
            if (event.get('subtype') != 'success' or event.get('is_error') is not False
                    or event.get('permission_denials', []) != []):
                raise ClaudeAdapterError('process_failed', events=self.count)
            final = event.get('result')
            if not isinstance(final, str) or not final.strip():
                raise ClaudeAdapterError('invalid_response', events=self.count)
            # The CLI result can contain just the last text block, while the
            # same API message has already emitted several complete blocks.
            parts = [self.blocks[key] for key in sorted(self.blocks)] if self.partial else self.parts
            if self.text and final not in (self.text, parts[-1] if parts else self.text):
                raise ClaudeAdapterError('invalid_response', events=self.count)
            if not self.text:
                self._text(final)
            self.result = event
            # init.model/modelUsage can be CLI-normalized request metadata.
            # Only a non-alias assistant response model is service evidence.
        elif kind not in ('system', 'rate_limit_event'):
            raise ClaudeAdapterError('invalid_response', events=self.count)

    def _stop_reason(self, reason):
        if reason == 'tool_use':
            raise ClaudeAdapterError('isolation_rejected', events=self.count)
        if reason in ('max_tokens', 'model_context_window_exceeded'):
            raise ClaudeAdapterError('invalid_response', events=self.count)

    def _text(self, value):
        if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_TEXT_BYTES:
            raise ClaudeAdapterError('invalid_response', events=self.count)
        if value != self.text:
            self.text = value
            self.callback({'type': 'text', 'text': value})


def run(prompt, cwd, on_event, cancel_event, route=None, *, images=None):
    if images not in (None, []):
        raise ValueError('Claude 当前连接仅接入文字，请选择支持图片的 AI。')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('Claude 对话上下文为空或过长。')
    if not isinstance(cwd, (str, Path)) or not Path(cwd).is_dir():
        raise ValueError('聊天工作目录不存在。')
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    route = validate_route(route if route is not None else capture_route())
    settings, connection, defaults = _settings()
    if settings['route_fingerprint'] != route['route_fingerprint']:
        raise ClaudeAdapterError('configuration_changed', phase='startup')
    command = [settings['binary'], '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--include-partial-messages', '--model', route['model'], '--permission-mode', 'plan',
        '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
        '--settings', json.dumps(dict(ISOLATION_SETTINGS, **defaults)), '--disable-slash-commands', '--no-chrome',
        '--no-session-persistence', '--system-prompt', '按本轮提供的共享上下文回答；仅返回文字。']
    payload = (json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt}},
                          ensure_ascii=False) + '\n').encode('utf-8')
    stream = _Stream(on_event)
    on_event({'type': 'status', 'text': '正在连接 Claude…'})
    try:
        _invoke(command, connection, payload, stream.accept,
                cancel=cancel_event, timeout=RUN_TIMEOUT_SECONDS)
    except InterruptedError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise ClaudeAdapterError('process_failed', phase='startup') from None
    if stream.result is None or not stream.text.strip():
        raise ClaudeAdapterError('invalid_response', events=stream.count)
    return {'text': stream.text, 'provider': PROVIDER, 'model': stream.actual or route['model'],
            'model_source': 'service_reported' if stream.actual else 'requested_route'}
