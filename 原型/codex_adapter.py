"""Use the existing authenticated Codex CLI, no global configuration changes."""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
import re
import tempfile

from model_catalog import entry, fingerprint, identifier, valid_model, image_capability
from profile_capabilities import model_effort_capability, validate_profile_effort


DEFAULT_MODEL = 'gpt-6.1-sol'
MAX_CONFIG_BYTES = 512 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def _home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def _read(path, limit):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('bounded configuration')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('bounded configuration')
    return data.decode('utf-8')


def _settings():
    """Read only top-level model settings; never load auth.json or provider keys."""
    values = {}
    path = _home() / 'config.toml'
    if path.exists():
        content = _read(path, MAX_CONFIG_BYTES)
        for line in content.splitlines():
            if line.strip().startswith('['):
                break
            match = re.fullmatch(r'\s*(model|model_provider|model_reasoning_effort)\s*=\s*("(?:[^"\\]|\\.)*"|\'[^\']*\')\s*(?:#.*)?', line)
            if match:
                value = match.group(2)
                values[match.group(1)] = json.loads(value) if value.startswith('"') else value[1:-1]
    model = values.get('model') or DEFAULT_MODEL
    provider = values.get('model_provider') or 'openai'
    if not valid_model(model) or provider != 'openai':
        raise ValueError('本机 Codex 的模型或提供商配置暂不支持此文本连接。')
    return {'default_model': model, 'configured_provider': provider,
            'configured_effort': values.get('model_reasoning_effort') or 'medium',
            'base_url': '', 'binary': executable() or '',
            'default_source': '本机默认配置' if values.get('model') else '小白兔文本连接回退默认'}


def _route_snapshot(settings):
    return fingerprint({key: settings[key] for key in ('default_model', 'configured_provider', 'base_url', 'binary')})


def _reasoning(row, settings):
    levels = row.get('supported_reasoning_levels') or row.get('supportedReasoningEfforts') or []
    supported = [item.get('effort', item.get('reasoningEffort')) for item in levels if isinstance(item, dict)]
    supported = [value for value in supported if value in REASONING_EFFORTS]
    default = row.get('default_reasoning_level') or row.get('defaultReasoningEffort')
    if supported:
        return 'medium' if 'medium' in supported else (default if default in supported else supported[0])
    return settings['configured_effort'] if settings['configured_effort'] in REASONING_EFFORTS else 'medium'


def _catalog_row(selected):
    try:
        rows = json.loads(_read(_home() / 'models_cache.json', MAX_CATALOG_BYTES)).get('models', [])
        if isinstance(rows, list):
            return next((row for row in rows[:200] if isinstance(row, dict)
                         and (row.get('slug') or row.get('model')) == selected), {})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def _image_capability(row):
    modalities = row.get('input_modalities', row.get('inputModalities'))
    if not isinstance(modalities, list) or not modalities or any(not isinstance(v, str) for v in modalities):
        return image_capability(source='Codex 本机缓存未确认图像输入能力')
    return image_capability('supported' if 'image' in modalities and 'text' in modalities else 'unsupported',
                            'Codex 本机模型缓存的能力声明；推理验收结果另记')


def capture_route(model=None, *, reasoning_effort=None):
    state = available()
    if not state.get('available'):
        raise ValueError(state.get('reason') or 'Codex 的本机连接暂不可用。')
    settings = _settings()
    selected = model if model is not None else settings['default_model']
    if not valid_model(selected):
        raise ValueError('Codex 模型选择无效。')
    row = _catalog_row(selected)
    capability = model_effort_capability('codex', row)
    selected_effort = validate_profile_effort(capability, reasoning_effort)
    route = dict(settings, backend='codex', model_id=identifier('codex', selected), model=selected,
                 provider='ChatGPT', route_fingerprint=_route_snapshot(settings),
                 reasoning_effort=selected_effort if selected_effort is not None else _reasoning(row, settings),
                 effort_selection='explicit' if selected_effort is not None else 'agent_default',
                 **_image_capability(row), **capability)
    if model is None:
        route['source'] = settings['default_source']
    return route


def model_catalog():
    state = available()
    try:
        settings = _settings()
    except (OSError, ValueError):
        return []
    try:
        cache = json.loads(_read(_home() / 'models_cache.json', MAX_CATALOG_BYTES))
        raw = cache.get('models', [])
        if not isinstance(raw, list):
            raw = []
    except (OSError, ValueError, TypeError):
        raw = []
    result, seen = [], set()
    hidden = {row.get('slug') or row.get('model') for row in raw[:200] if isinstance(row, dict)
              and (row.get('hidden') is True or row.get('visibility') in ('hide', 'hidden'))
              and valid_model(row.get('slug') or row.get('model'))}
    for row in raw[:200]:
        if not isinstance(row, dict) or row.get('visibility') != 'list' or row.get('hidden') is True:
            continue
        model = row.get('slug') or row.get('model')
        modalities = row.get('input_modalities') or row.get('inputModalities')
        if not valid_model(model) or model in seen or model in hidden or (modalities is not None and 'text' not in modalities):
            continue
        seen.add(model)
        # Directory identity survives native default changes. The accepted
        # route captures and validates its fresh full settings fingerprint.
        route = dict(settings,
                     reasoning_effort=_reasoning(row, settings), **_image_capability(row))
        name = row.get('display_name') or row.get('displayName') or model
        if not isinstance(name, str) or len(name) > 120:
            name = model
        result.append(entry('codex', model, state, name=name, source='Codex 本机模型缓存',
                            is_default=model == settings['default_model'], route=route, **_image_capability(row),
                            effort_capability=model_effort_capability('codex', row),
                            detail='本机目录候选；是否允许本次请求由模型服务决定。' +
                            (' 本机未显式配置模型；跟随默认使用小白兔文本连接回退模型。'
                             if model == settings['default_model'] and settings['default_source'] != '本机默认配置' else '')))
    if settings['default_model'] not in seen:
        model = settings['default_model']
        detail = ('沿用本机默认模型；当前目录未确认该项。' if settings['default_source'] == '本机默认配置'
                  else '本机未显式配置模型；使用小白兔文本连接回退模型，未确认 Codex 原生默认。')
        result.insert(0, entry('codex', model, state, is_default=True, source=settings['default_source'],
                              route=dict(settings,
                                         reasoning_effort=_reasoning({}, settings), **_image_capability(_catalog_row(model))),
                              effort_capability=model_effort_capability('codex', _catalog_row(model)),
                              detail=detail, selectable=model not in hidden, **_image_capability(_catalog_row(model))))
    return result


def validate_route(route):
    settings = _settings()
    if (not isinstance(route, dict) or route.get('backend') != 'codex'
            or route.get('provider') != 'ChatGPT' or not valid_model(route.get('model'))
            or route.get('model_id') != identifier('codex', route['model'])
            or route.get('route_fingerprint') != _route_snapshot(settings)
            or route.get('base_url') != ''
            or route.get('effort_selection', 'agent_default') not in ('explicit', 'agent_default')
            or route.get('reasoning_effort') not in REASONING_EFFORTS):
        raise ValueError('Codex 的本机模型或提供商配置已变化，请刷新后重新发送。')
    if route.get('effort_selection') == 'explicit':
        validate_profile_effort(model_effort_capability('codex', _catalog_row(route['model'])), route['reasoning_effort'])
    state = available()
    if not state.get('available'):
        raise ValueError(state.get('reason') or 'Codex 的本机连接暂不可用。')
    return dict(route)


def executable():
    for candidate in (os.environ.get('ONE_AI_CODEX'),
                      '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex',
                      shutil.which('codex')):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def available():
    binary = executable()
    if not binary:
        return {'available': False, 'reason': '没有找到 Codex CLI。'}
    try:
        check = subprocess.run([binary, 'login', 'status'], capture_output=True, text=True, timeout=12)
        if check.returncode:
            return {'available': False, 'reason': 'Codex 需要先在本机登录。'}
        output = (check.stdout or '') + '\n' + (check.stderr or '')
        if 'Logged in using ChatGPT' not in output:
            return {'available': False, 'reason': '当前文本连接需要本机 Codex 的 ChatGPT 登录。'}
        settings = _settings()
        return {'available': True, 'reason': '', 'model': settings['default_model'], 'provider': 'ChatGPT'}
    except ValueError:
        return {'available': False, 'reason': '本机 Codex 的模型或提供商配置暂不支持此文本连接。'}
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'reason': 'Codex CLI 检查未完成。'}


def stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run(prompt, cwd, on_event, cancel_event, route=None, *, images=None):
    if images is None or images == []:
        return _run(prompt, cwd, on_event, cancel_event, route)
    from image_inputs import read_images
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    snapshots = read_images(images)
    if route is None:
        from model_catalog import ModelCatalog
        route = ModelCatalog({'codex': __import__(__name__)}).resolve('codex')
    if (not isinstance(route, dict) or route.get('image_input') != 'supported'
            or _image_capability(_catalog_row(route.get('model')))['image_input'] != 'supported'):
        raise ValueError('所选 Codex 模型尚未确认支持图片，请刷新后选择支持图片的模型。')
    with tempfile.TemporaryDirectory(prefix='rabbit-codex-images-', dir='/private/tmp') as name:
        directory = Path(name)
        directory.chmod(0o700)
        paths = []
        for position, image in enumerate(snapshots, 1):
            path = directory / ('image-' + str(position) + ('.png' if image['mime'] == 'image/png' else '.jpg'))
            with path.open('xb') as handle:
                handle.write(image['data'])
            path.chmod(0o600)
            paths.append(str(path))
        return _run(prompt, cwd, on_event, cancel_event, route, image_paths=paths)


def _run(prompt, cwd, on_event, cancel_event, route=None, *, image_paths=None):
    if cancel_event.is_set():
        raise InterruptedError('已停止这次回答。')
    binary = executable()
    if not binary:
        raise RuntimeError('没有找到 Codex CLI。')
    if route is None:
        from model_catalog import ModelCatalog
        route = ModelCatalog({'codex': __import__(__name__)}).resolve('codex')
    route = validate_route(route)
    command = [binary, 'exec', '--json', '--ephemeral', '--ignore-user-config',
               '--skip-git-repo-check', '--sandbox', 'read-only', '--color', 'never',
               '--model', route['model'], '-C', str(cwd),
               '--disable', 'shell_tool', '--disable', 'unified_exec',
               '--disable', 'browser_use', '--disable', 'computer_use',
               '--disable', 'image_generation', '--disable', 'multi_agent',
               '--disable', 'apps', '--disable', 'plugins', '--disable', 'memories',
               '--disable', 'hooks', '--disable', 'goals', '--disable', 'sleep_tool',
               '--disable', 'skill_search', '--disable', 'skill_mcp_dependency_install',
               '--enable', 'skip_host_skill_discovery',
               '-c', 'approval_policy="never"', '-c', 'model_reasoning_effort=' + json.dumps(route['reasoning_effort']),
               '-c', 'model_provider="openai"',
               '-c', 'web_search="disabled"', '-']
    if image_paths:
        # The following --json ends clap's variadic image arguments. Paths are
        # our private verified copies, never user paths or shell interpolation.
        command[2:2] = [value for path in image_paths for value in ('--image', path)]
    # Prompt goes through stdin, never shell interpolation or process arguments.
    process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace', start_new_session=True)
    events = queue.Queue()
    errors = []

    def stdout_reader():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    def stderr_reader():
        for line in process.stderr:
            # Retain only a small tail, never emit progress to public logs.
            errors.append(line[-1000:])
            if len(errors) > 8:
                del errors[:-8]

    readers = [threading.Thread(target=stdout_reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
    for reader in readers:
        reader.start()
    output, native_id, failure = '', None, None
    completed = False
    started = time.monotonic()
    try:
        if cancel_event.is_set():
            raise InterruptedError('已停止这次回答。')
        process.stdin.write(prompt)
        process.stdin.close()
        on_event({'type': 'status', 'text': 'Codex 正在回答'})
        while True:
            if cancel_event.is_set():
                stop_process(process)
                raise InterruptedError('已停止这次回答。')
            if time.monotonic() - started > 600:
                stop_process(process)
                raise TimeoutError('Codex 回答超过等待时间，已停止。可以检查后重新发送。')
            try:
                line = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get('type')
            if kind == 'thread.started':
                native_id = event.get('thread_id')
            if kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                text = event['item'].get('text', '')
                if text:
                    output = text
                    on_event({'type': 'text', 'text': output})
            elif kind == 'turn.completed':
                completed = True
            elif kind in ('turn.failed', 'error'):
                error = event.get('error') or event.get('message') or 'Codex 未完成回答。'
                failure = error.get('message', str(error)) if isinstance(error, dict) else str(error)
        return_code = process.wait(timeout=10)
        if cancel_event.is_set():
            raise InterruptedError('已停止这次回答。')
        if return_code or failure or not completed:
            raise RuntimeError(failure or 'Codex 未完成回答。请检查本机登录或网络连接。')
        if not output.strip():
            raise RuntimeError('Codex 完成后没有返回可显示的正文。')
        return {'text': output, 'native_session_id': native_id, 'model': route['model'],
                'provider': 'ChatGPT', 'model_source': 'requested_route'}
    finally:
        stop_process(process)
        if not process.stdin.closed:
            process.stdin.close()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
