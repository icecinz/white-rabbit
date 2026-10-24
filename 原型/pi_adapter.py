"""Disposable Pi text turns; native settings/auth are read by the SDK worker.

The application owns conversation context. Pi resources and persistence are
empty in-memory objects. Credentials never enter argv, the route or stdout.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from model_catalog import entry, identifier, image_capability, valid_model
import rabbit_runtime.process as agent_process
from profile_capabilities import model_effort_capability, validate_profile_effort


WORKER = Path(__file__).with_name('pi_worker.mjs')
RUN_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 20
CANCEL_GRACE_SECONDS = 2
MAX_PROMPT_BYTES = 1_000_000
MAX_LINE_BYTES = 2_100_000
MAX_OUTPUT_BYTES = 8_000_000
MAX_TEXT_BYTES = 2_000_000
RETAINED_ENV = frozenset(('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'))
_cache, _cache_lock = None, threading.Lock()


class PiAdapterError(RuntimeError):
    def __init__(self, code, *, phase='response'):
        self.safe_diagnostic = {'code': code, 'phase': phase}
        super().__init__({'configuration_changed': 'Pi 的本机连接配置已变化，请刷新后重新发送。',
            'configuration_invalid': 'Pi 的本机配置暂不能用于独立文字连接。',
            'timeout': 'Pi 响应超时，请稍后重试。',
            'isolation_rejected': 'Pi 的文字隔离检查未通过。',
            'cleanup_unconfirmed': 'Pi 请求已中止，但后台进程终止尚未确认。',
            'invalid_response': 'Pi 未返回完整的文字回答。',
            'process_failed': 'Pi 请求未完成，请检查本机连接。'}.get(code,
            'Pi 本机连接检查未完成。'))


def _installation():
    candidate = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    node = os.environ.get('ONE_AI_PI_NODE') or (str(candidate) if candidate.is_file() else shutil.which('node'))
    package = os.environ.get('ONE_AI_PI_PACKAGE')
    if not package:
        cli = shutil.which('pi')
        if cli:
            resolved = Path(cli).resolve()
            if resolved.name == 'cli.js' and resolved.parent.name == 'dist':
                package = str(resolved.parent.parent)
    if not package:
        package = str(Path.home() / '.npm-global/lib/node_modules/@earendil-works/pi-coding-agent')
    home = os.environ.get('PI_CODING_AGENT_DIR') or str(Path.home() / '.pi/agent')
    if (not node or not Path(node).is_file() or not os.access(node, os.X_OK)
            or not Path(package, 'dist/index.js').is_file() or not WORKER.is_file()):
        raise PiAdapterError('configuration_invalid', phase='startup')
    return {'node': str(Path(node).resolve()), 'package': str(Path(package).resolve()),
            'agent_dir': str(Path(home).expanduser().absolute())}


def _environment(home):
    env = {key: value for key, value in os.environ.items() if key in RETAINED_ENV}
    env.update(HOME=home, PI_CODING_AGENT_DIR=str(Path(home) / '.pi/agent'),
               PI_OFFLINE='1', NO_COLOR='1')
    return env


def _invoke(payload, callback=None, cancel=None):
    installation = _installation()
    request = dict(payload, package=installation['package'], agent_dir=installation['agent_dir'],
                   binary=installation['node'])
    serialized = (json.dumps(request, ensure_ascii=False) + '\n').encode()
    result, final = None, False
    def accept(event):
        nonlocal result, final
        if not isinstance(event, dict) or final:
            raise PiAdapterError('invalid_response')
        kind = event.get('type')
        if kind == 'text':
            value = event.get('text')
            if not isinstance(value, str) or len(value.encode()) > MAX_TEXT_BYTES:
                raise PiAdapterError('invalid_response')
            if callback:
                callback({'type': 'text', 'text': value})
        elif kind == 'result' and isinstance(event.get('result'), dict):
            result, final = event['result'], True
        elif kind == 'error':
            code = event.get('code')
            raise PiAdapterError(code if code in ('configuration_invalid', 'configuration_changed',
                'isolation_rejected', 'invalid_response', 'process_failed') else 'process_failed')
        else:
            raise PiAdapterError('invalid_response')
    with tempfile.TemporaryDirectory(prefix='rabbit-pi-', dir='/private/tmp') as home:
        Path(home).chmod(0o700)
        try:
            agent_process.invoke([installation['node'], str(WORKER)], _environment(home), serialized,
                accept, cancel=cancel,
                timeout=PROBE_TIMEOUT_SECONDS if request['operation'] != 'run' else RUN_TIMEOUT_SECONDS,
                cwd=home, cancel_payload=b'{"operation":"cancel"}\n', cancel_grace=CANCEL_GRACE_SECONDS,
                max_input=MAX_PROMPT_BYTES + 16000, max_line=MAX_LINE_BYTES, max_output=MAX_OUTPUT_BYTES)
        except agent_process.ProcessError as error:
            raise PiAdapterError(error.safe_diagnostic['code']) from None
    if not final or result is None:
        raise PiAdapterError('invalid_response')
    return result


def _probe(*, fresh=False):
    # The worker rechecks exact file identities before every route/run. Probe
    # caching only reduces catalog reads; it never authorizes an old route.
    global _cache
    installation = _installation()
    with _cache_lock:
        if _cache and not fresh and _cache[0] == installation and time.monotonic() - _cache[1] < 10:
            return _cache[2]
        result = _invoke({'operation': 'probe'})
        _cache = (installation, time.monotonic(), result)
        return result


def available():
    try:
        value = _probe()
        return {'available': True, 'reason': '', 'model': value['default_model'], 'provider': value['provider']}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError):
        return {'available': False, 'reason': 'Pi 本机文字连接暂不可用，请检查安装和配置。'}


def _route(value, model):
    if model not in value['models'] or not valid_model(model):
        raise ValueError('Pi 模型选择无效，请刷新目录。')
    return {key: value[key] for key in ('provider', 'configured_provider', 'base_url',
        'binary', 'route_fingerprint', 'default_model', 'reasoning_effort')} | {
        'backend': 'pi', 'model': model, 'model_id': identifier('pi', model),
        'base_url': value['model_endpoints'][model],
        'effort_selection': 'agent_default',
        'source': ('本机 Pi 模型；本机未配置思考强度，跟随 SDK 默认，实际由模型决定'
                   if value['reasoning_effort'] is None else
                   '本机 Pi 模型；沿用已配置思考强度，实际由 SDK 和模型决定'),
        **image_capability('unsupported', 'Pi 当前独立连接仅接入文字'),
        **model_effort_capability('pi', {})}


def capture_route(model=None, *, reasoning_effort=None):
    validate_profile_effort(model_effort_capability('pi', {}), reasoning_effort)
    value = _probe(fresh=True)
    return _route(value, value['default_model'] if model is None else model)


def validate_route(route):
    if not isinstance(route, dict):
        raise PiAdapterError('configuration_changed', phase='startup')
    _invoke({'operation': 'validate', 'route': route})
    return dict(route)


def model_catalog():
    try:
        value = _probe()
        state = {'available': True, 'provider': value['provider']}
        return [entry('pi', model, state, source='Pi 内置模型目录；本机已配置连接',
            is_default=model == value['default_model'], route=_route(value, model),
            detail='连接检查只验证本机元数据；候选模型的实际推理尚需验证。',
            image_input='unsupported', image_input_source='Pi 当前独立连接仅接入文字')
            for model in value['models'][:200]]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError):
        return []


def run(prompt, cwd, on_event, cancel_event, route=None, *, images=None):
    if images not in (None, []):
        raise ValueError('Pi 当前连接仅接入文字，请选择支持图片的 AI。')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise ValueError('Pi 对话上下文为空或过长。')
    if not isinstance(cwd, (str, Path)) or not Path(cwd).is_dir():
        raise ValueError('聊天工作目录不存在。')
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    route = route if route is not None else capture_route()
    on_event({'type': 'status', 'text': '正在连接 Pi…'})
    try:
        result = _invoke({'operation': 'run', 'route': route, 'prompt': prompt}, on_event, cancel_event)
    except InterruptedError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise PiAdapterError('process_failed', phase='startup') from None
    if (not isinstance(result.get('text'), str) or not result['text'].strip()
            or len(result['text'].encode()) > MAX_TEXT_BYTES
            or not valid_model(result.get('model')) or result.get('provider') != route.get('provider')
            or result.get('model_source') not in ('requested_route', 'service_reported')):
        raise PiAdapterError('invalid_response')
    return {key: result[key] for key in ('text', 'model', 'model_source', 'provider')}
