"""Independent text connector to the installed official DSH SDK.

Native settings and managed credentials stay read-only. The child owns one
in-memory Agent; the application owns the only durable conversation history.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from rabbit_runtime.process import invoke, ProcessError
from model_catalog import entry, fingerprint, identifier, image_capability, valid_model
from profile_capabilities import model_effort_capability, validate_profile_effort

PROVIDER = 'deepseek-official'
RUN_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 12
MAX_PROMPT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
_WORKER = Path(__file__).with_name('dsh_worker.mjs')
_probe_lock = threading.RLock()
_probe_cache = None
_MESSAGES = {
    'sdk_unavailable': 'DSH 的本机 SDK 或 Node 不可用。',
    'sdk_unsupported': '这版 DSH 尚未完成接入核验。',
    'invalid_configuration': 'DSH 原有连接配置不可读取或格式无效。',
    'unsupported_configuration': 'DSH 当前文字连接尚未支持这份原有配置。',
    'configuration_changed': 'DSH 连接配置已变化，请刷新后重发。',
    'connection_unavailable': 'DSH 尚未配置可用的本机托管凭据。',
    'isolation_rejected': 'DSH 返回了当前文字连接不支持的工具或额外上下文。',
    'inference_failed': 'DSH 本轮没有完整完成；可以检查原有连接后重发。',
    'invalid_response': 'DSH 本轮返回不完整或超过限制。',
    'bridge_failed': 'DSH 本轮连接未完成。',
    'timeout': 'DSH 本轮连接超时。',
    'process_failed': 'DSH 本轮进程未正常完成。',
    'cleanup_unconfirmed': 'DSH 后台进程终止尚未确认。',
}


class DshAdapterError(RuntimeError):
    def __init__(self, code, *, phase='stream'):
        code = code if code in _MESSAGES else 'bridge_failed'
        super().__init__(_MESSAGES[code])
        self.safe_diagnostic = {'code': code, 'phase': phase, 'connector': 'dsh'}


def _home():
    return Path(os.environ.get('DSH_HOME') or Path.home() / '.dsh').expanduser().absolute()


def _package():
    explicit = os.environ.get('ONE_AI_DSH_PACKAGE')
    if explicit:
        return Path(explicit).expanduser().resolve()
    binary = shutil.which('dsh')
    if binary:
        resolved = Path(binary).resolve()
        if resolved.parent.name == 'lib':
            return resolved.parent.parent
    return Path.home() / '.npm-global/lib/node_modules/@deepseek-ai/dsh'


def _node():
    explicit = os.environ.get('ONE_AI_NODE')
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    return str(bundled) if bundled.is_file() else shutil.which('node')


def _stamp():
    paths = [_home() / 'settings.yaml', _home() / '.credentials.yaml', _package() / 'package.json', _WORKER]
    values = []
    for path in paths:
        try:
            stat = path.lstat()
            values.append([str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
        except FileNotFoundError:
            values.append([str(path), None])
    return fingerprint([values, _node()])


def _environment(private_home):
    allowed = ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
               'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy')
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(HOME=private_home, DSH_HOME=str(Path(private_home) / '.dsh'), DSH_TELEMETRY_DISABLED='1')
    return env


def _call(mode, payload, consumer, *, cancel=None, timeout):
    node = _node()
    if not node or not Path(node).is_file():
        raise DshAdapterError('sdk_unavailable', phase='startup')
    with tempfile.TemporaryDirectory(prefix='rabbit-dsh-', dir='/private/tmp') as private:
        Path(private).chmod(0o700)
        try:
            invoke([node, str(_WORKER), str(_package()), str(_home()), mode], _environment(private),
                   payload, consumer, cancel=cancel, timeout=timeout, cwd=private,
                   max_input=MAX_PROMPT_BYTES + 4096, max_line=MAX_TEXT_BYTES + 4096,
                   max_output=8 * 1024 * 1024)
        except InterruptedError:
            raise
        except ProcessError as error:
            code = getattr(error, 'safe_diagnostic', {}).get('code')
            raise DshAdapterError(code, phase='startup' if mode == 'inspect' else 'stream') from None
        except OSError:
            raise DshAdapterError('process_failed', phase='startup') from None


def _inspect():
    global _probe_cache
    stamp = _stamp()
    with _probe_lock:
        if _probe_cache and _probe_cache[0] == stamp and time.monotonic() - _probe_cache[1] < 15:
            return dict(_probe_cache[2])
        events = []
        def accept(event):
            if event.get('type') == 'error':
                raise DshAdapterError(event.get('code'), phase='startup')
            events.append(event)
        _call('inspect', None, accept, timeout=PROBE_TIMEOUT_SECONDS)
        if len(events) != 1:
            raise DshAdapterError('invalid_response', phase='startup')
        state = events[0]
        if (state.get('type') != 'inspection' or type(state.get('available')) is not bool
                or state.get('provider') != PROVIDER or not valid_model(state.get('default_model'))
                or state.get('reasoning_effort') not in ('off', 'high', 'max')
                or not isinstance(state.get('models'), list) or not 0 < len(state['models']) <= 101
                or any(not valid_model(model) for model in state['models'])
                or not isinstance(state.get('route_fingerprint'), str) or len(state['route_fingerprint']) != 64
                or not isinstance(state.get('base_url'), str) or len(state['base_url']) > 2048):
            raise DshAdapterError('invalid_response', phase='startup')
        if _stamp() != stamp:
            raise DshAdapterError('configuration_changed', phase='startup')
        _probe_cache = (stamp, time.monotonic(), dict(state))
        return dict(state)


def available():
    try:
        state = _inspect()
        return {'available': state['available'], 'reason': '' if state['available'] else _MESSAGES['connection_unavailable'],
                'model': state['default_model'], 'provider': PROVIDER}
    except (OSError, ValueError, RuntimeError):
        return {'available': False, 'reason': 'DSH 本机连接检查未完成。'}


def capture_route(model=None, *, reasoning_effort=None):
    validate_profile_effort(model_effort_capability('dsh', {}), reasoning_effort)
    state = _inspect()
    if not state['available']:
        raise ValueError(_MESSAGES['connection_unavailable'])
    selected = state['default_model'] if model is None else model
    if selected not in state['models']:
        raise ValueError('DSH 模型选择无效，请刷新目录。')
    return {key: state[key] for key in ('provider','default_model','base_url','configured_provider',
            'reasoning_effort','route_fingerprint','source')} | {
        'backend': 'dsh', 'model': selected, 'model_id': identifier('dsh', selected), 'binary': _node(),
        'effort_selection': 'agent_default', **image_capability('unsupported', 'DSH 当前连接仅接入文字'),
        **model_effort_capability('dsh', {})}


def validate_route(route):
    if not isinstance(route, dict):
        raise DshAdapterError('configuration_changed', phase='startup')
    current = capture_route(route.get('model'))
    fields = ('backend','model','model_id','provider','default_model','base_url','configured_provider',
              'binary','reasoning_effort','route_fingerprint','effort_selection')
    if any(route.get(key) != current[key] for key in fields):
        raise DshAdapterError('configuration_changed', phase='startup')
    return current


def model_catalog():
    try:
        state = _inspect()
    except (OSError, ValueError, RuntimeError):
        return []
    readiness = {'available': state['available'], 'provider': PROVIDER}
    result = []
    for model in state['models']:
        route = {key: state[key] for key in ('provider','default_model','base_url','configured_provider',
                 'reasoning_effort','route_fingerprint','source')}
        route['binary'] = _node()
        result.append(entry('dsh', model, readiness, source=state['source'],
            is_default=model == state['default_model'], route=route,
            detail='目录来自 DSH 本机配置；连接检查不代表所有候选模型已通过推理。',
            image_input='unsupported', image_input_source='DSH 当前连接仅接入文字'))
    return result


class _Stream:
    def __init__(self, callback, cancel=None):
        self.callback, self.ready, self.result, self.text = callback, False, None, ''
        self.cancel, self.last_emit, self.emitted_text = cancel, None, ''

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError('已停止这次回答。')

    def _publish(self, force=False):
        self._check_cancel()
        now = time.monotonic()
        if self.text != self.emitted_text and (force or self.last_emit is None or now - self.last_emit >= .1):
            self.last_emit, self.emitted_text = now, self.text
            self.callback({'type': 'text', 'text': self.text})
            self._check_cancel()

    def accept(self, event):
        self._check_cancel()
        kind = event.get('type')
        if kind == 'error':
            raise DshAdapterError(event.get('code'))
        if kind == 'ready' and not self.ready and self.result is None:
            if event.get('tools') != [] or event.get('native_persistence') is not False or event.get('extra_context') is not False:
                raise DshAdapterError('isolation_rejected', phase='startup')
            self.ready = True
            return
        if not self.ready or self.result is not None:
            raise DshAdapterError('invalid_response')
        if kind == 'text_delta':
            delta = event.get('text')
            if not isinstance(delta, str) or len((self.text + delta).encode('utf-8')) > MAX_TEXT_BYTES:
                raise DshAdapterError('invalid_response')
            self.text += delta
            self._publish()
        elif kind == 'result':
            if (event.get('finish') != 'completed' or event.get('text') != self.text or not self.text.strip()
                    or event.get('provider') != PROVIDER or not valid_model(event.get('model'))
                    or event.get('model_source') != 'requested_route'):
                raise DshAdapterError('invalid_response')
            self._publish(force=True)
            self.result = event
        else:
            raise DshAdapterError('invalid_response')


def run(prompt, cwd, on_event, cancel_event, route=None, *, images=None):
    if images not in (None, []):
        raise ValueError('DSH 当前连接仅接入文字，请选择支持图片的 AI。')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('DSH 对话上下文为空或过长。')
    if not isinstance(cwd, (str, Path)) or not Path(cwd).is_dir():
        raise ValueError('聊天工作目录不存在。')
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    accepted = validate_route(route if route is not None else capture_route())
    payload = json.dumps({'prompt': prompt, 'model': accepted['model'],
                          'fingerprint': accepted['route_fingerprint']}, ensure_ascii=False).encode('utf-8')
    stream = _Stream(on_event, cancel_event)
    on_event({'type': 'status', 'text': '正在连接 DSH…'})
    _call('run', payload, stream.accept, cancel=cancel_event, timeout=RUN_TIMEOUT_SECONDS)
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    if stream.result is None or stream.result['model'] != accepted['model']:
        raise DshAdapterError('invalid_response')
    return {key: stream.result[key] for key in ('text','provider','model','model_source')}
