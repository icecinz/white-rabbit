"""Isolated text/native-image Hermes bridge using the installed AIAgent API.

Each turn gets a private, disposable HERMES_HOME under /private/tmp.  Only the
native default or explicitly selected Ark API-key route is read from the original home;
accounts, history, memories, skills, MCP servers and hooks are never copied.
The supplied prompt contains the shared conversation. Native session IDs are
informational: the disposable Hermes sessions cannot be resumed.
"""

from __future__ import annotations

import contextlib
import base64
import io
import json
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from model_catalog import entry, fingerprint, identifier, valid_model, image_capability, file_capability
from image_inputs import metadata as image_metadata, read_images, ImageInputError, IMAGE_ERRORS


SOURCE_HOME = Path.home() / ".hermes"
INSTALLATION = SOURCE_HOME / "hermes-agent"
RUN_TIMEOUT_SECONDS = 240
PROBE_TIMEOUT_SECONDS = 20
MAX_PROMPT_BYTES = 1_000_000
IMAGE_UNSUPPORTED = '所选 Hermes 模型尚未确认支持图片，请选择支持图片的模型。'
IMAGE_ISOLATION_ERROR = 'Hermes 原生图片隔离验证失败。'
ARK_BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
# Exact Chat API IDs from Ark's official model/Deep Thinking documentation.
# These are documentary candidates, not account entitlement or an App connection.
ARK_MODELS = (('doubao-seed-2-1-lite-260915', '豆包 Seed 2.1 Lite'),
              ('doubao-seed-2-1-pro-260915', '豆包 Seed 2.1 Pro'))
ARK_SOURCE = '火山方舟官方模型表 / Chat API（2026-10-14）'
ROUTE_CHANGED = 'Hermes 的本机模型或提供商配置已变化，请刷新后重新发送。'
TEXT_ISOLATION_ERROR = 'Hermes 文字模型路线隔离验证失败。'
_PUBLIC_ERRORS = {"模型配置不可用", "模型凭据不可用", "此认证方式暂不支持隔离调用",
                  "Hermes API 初始化失败", "Hermes 未返回完整回答",
                  "Hermes 模型请求失败", "Hermes 工具隔离验证失败",
                  "Hermes 无法连接模型服务", "Hermes 模型认证失败",
                  "Hermes 模型额度不足", "Hermes 模型请求被限流",
                  "Hermes 模型服务响应超时", "Hermes 的本机模型或提供商配置已变化，请刷新后重新发送。",
                  "Hermes 的请求模型被本机归一化改变，请刷新后重新选择。"}
_PUBLIC_ERRORS.update(IMAGE_ERRORS)
_PUBLIC_ERRORS.update((IMAGE_UNSUPPORTED, IMAGE_ISOLATION_ERROR))
_PUBLIC_ERRORS.add(TEXT_ISOLATION_ERROR)


def _python() -> Path | None:
    for candidate in (INSTALLATION / "venv/bin/python", INSTALLATION / ".venv/bin/python"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _environment(temporary_home: Path) -> dict[str, str]:
    # Do not inherit provider keys, agent controls, preload prompts or plugin paths.
    retained = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    environment = {key: value for key, value in os.environ.items() if key in retained}
    environment.update({"HERMES_HOME": str(temporary_home), "HERMES_SAFE_MODE": "1",
                        "HERMES_IGNORE_RULES": "1", "HERMES_SINGLE_QUERY_SESSION": "1",
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                        "PYTHONUNBUFFERED": "1"})
    return environment


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _invoke(*, probe: bool, prompt: str = "", cwd: str = "",
            on_event: Callable[[dict], None] | None = None,
            cancel_event: threading.Event | None = None, route: dict | None = None,
            catalog: bool = False, capture: bool = False, selected_model=None, images=None) -> dict:
    executable = _python()
    if executable is None or not (INSTALLATION / "run_agent.py").is_file():
        raise RuntimeError("未找到可用的本机 Hermes 安装。")
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Hermes 请求已取消。")
    callback = on_event or (lambda event: None)
    with tempfile.TemporaryDirectory(prefix="unified-hermes-", dir="/private/tmp") as name:
        temporary_home = Path(name)
        temporary_home.chmod(0o700)
        command = [str(executable), "-B", str(Path(__file__).resolve()), "--worker"]
        payload = {"probe": probe, "prompt": prompt, "cwd": cwd,
                   "installation": str(INSTALLATION), "source_home": str(SOURCE_HOME),
                   "route": route, "catalog": bool(catalog),
                   "capture": bool(capture), "selected_model": selected_model}
        if images is not None:
            resolved_images = image_metadata(images)
            if resolved_images:
                payload['images'] = resolved_images
        process = subprocess.Popen(command, cwd=temporary_home, env=_environment(temporary_home),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   start_new_session=True)
        output: queue.Queue = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict):
                        output.put(event)
            finally:
                output.put(None)

        reader = threading.Thread(target=read_output, name="hermes-output", daemon=True)
        reader.start()
        result = None
        timeout = PROBE_TIMEOUT_SECONDS if probe else RUN_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
            while True:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Hermes 请求已取消。")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Hermes 响应超时，请稍后重试。")
                try:
                    event = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event is None:
                    break
                if event.get("type") in {"status", "text"} and isinstance(event.get("text"), str):
                    callback({"type": event["type"], "text": event["text"]})
                elif event.get("type") == "result" and isinstance(event.get("result"), dict):
                    result = event["result"]
                elif event.get("type") == "error":
                    # Library stderr and exception bodies may contain credentials or context.
                    # Only the worker's fixed, public error category crosses the boundary.
                    reason = event.get("reason")
                    raise RuntimeError(reason if reason in _PUBLIC_ERRORS else "Hermes 调用失败。")
            process.wait(timeout=2)
            if result is None or process.returncode != 0:
                raise RuntimeError("Hermes 调用失败，请检查本机 Hermes 安装和模型连接。")
            return result
        finally:
            _terminate(process)
            reader.join(timeout=2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


def available() -> dict:
    """Read-only model/auth check; this does not send a model request."""
    try:
        return _invoke(probe=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        reason = str(error)
        if reason not in _PUBLIC_ERRORS:
            reason = "本机 Hermes 暂不可用；请检查安装或模型配置。"
        return {"available": False, "reason": reason}


def model_catalog() -> list:
    """Native-provider directory plus declared Ark documentary candidates."""
    try:
        result = _invoke(probe=True, catalog=True)
        models = result.get('models', [])
        return models if isinstance(models, list) else []
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []


def validate_route(route) -> dict:
    _invoke(probe=True, route=route)
    return dict(route)


def capture_route(model=None) -> dict:
    return _invoke(probe=True, capture=True, selected_model=model)


def run(prompt: str, cwd: str, on_event: Callable[[dict], None],
        cancel_event: threading.Event, route: dict | None = None, *, images=None) -> dict:
    """Run selected ordered images with shared text; text events are snapshots."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Hermes 消息不能为空。")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("Hermes 对话上下文过长，请新建聊天后重试。")
    if not Path(cwd).is_dir():
        raise ValueError("聊天工作目录不存在。")
    resolved_images = image_metadata(images)
    if resolved_images:
        if cancel_event.is_set():
            raise RuntimeError('Hermes 请求已取消。')
        if route is None:
            route = capture_route()
        if not isinstance(route, dict) or route.get('image_input') != 'supported':
            raise ValueError(IMAGE_UNSUPPORTED)
    on_event({"type": "status", "text": "正在连接 Hermes…"})
    result = _invoke(probe=False, prompt=prompt, cwd=cwd, on_event=on_event,
                     cancel_event=cancel_event, route=route,
                     **({'images': resolved_images} if resolved_images else {}))
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Hermes 未返回完整回答")
    on_event({"type": "text", "text": text})
    output = {key: result[key] for key in ("text", "native_session_id", "model", "provider") if key in result}
    if isinstance(output.get('model'), str) and output['model']:
        output['model_source'] = 'adapter_reported'
    return output


class _WorkerError(Exception):
    pass


def _request_failure_reason(error) -> str:
    """Classify locally; never return arbitrary provider error bodies."""
    description = str(error).lower()
    if any(term in description for term in ("insufficient", "billing", "credit balance", "402")):
        return "Hermes 模型额度不足"
    if any(term in description for term in ("rate limit", "ratelimit", "429")):
        return "Hermes 模型请求被限流"
    if any(term in description for term in ("unauthorized", "authentication", "invalid api key", "401", "403")):
        return "Hermes 模型认证失败"
    if any(term in description for term in ("timeout", "timed out")):
        return "Hermes 模型服务响应超时"
    if any(term in description for term in ("connection", "connecterror", "dns", "network", "name resolution")):
        return "Hermes 无法连接模型服务"
    return "Hermes 模型请求失败"


def _snapshot(model):
    return fingerprint({'default_model': model['configured_default'],
                        'provider': model['provider'], 'base_url': model['base_url']})


def _native_text(path, limit, *, optional=False):
    """Read one bounded ordinary native file without following links or FIFOs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        if optional:
            return ''
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('bounded native file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            body = stream.read(limit + 1)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if (len(body) > limit or identity(before) != identity(after)
                or identity(after) != identity(named)):
            raise ValueError('native file changed')
        return body.decode('utf-8')
    finally:
        os.close(fd)


def _worker_model(source_home: Path, temporary_home: Path, route=None, *, images=None,
                  selected_model=None) -> dict:
    """Read only the selected route's declared key; never load the auth store."""
    import re
    import yaml
    from dotenv import dotenv_values

    key = selected_model
    if route is not None:
        if (not isinstance(route, dict) or route.get('backend') != 'hermes'
                or not isinstance(route.get('model_id'), str)
                or not route['model_id'].startswith('local/hermes/')):
            raise _WorkerError(ROUTE_CHANGED)
        key = route['model_id'][len('local/hermes/'):]
    if key is not None and not valid_model(key):
        raise _WorkerError('模型配置不可用')
    ark_selected = isinstance(key, str) and key.startswith('volcengine/')
    if ark_selected:
        model = key[len('volcengine/'):]
        if model not in {row[0] for row in ARK_MODELS}:
            raise _WorkerError('模型配置不可用')
        provider, model_config, configured_default = 'volcengine', {}, ''
    else:
        try:
            source_config = yaml.safe_load(_native_text(source_home / 'config.yaml', 512 * 1024))
            model_config = source_config.get("model", {})
            if not isinstance(model_config, dict):
                raise ValueError("model")
            provider = str(model_config.get("provider", "")).strip()
            model = str(model_config.get("default", "")).strip()
            if not provider or provider == "auto" or not valid_model(model):
                raise ValueError("model")
        except Exception:
            raise _WorkerError("模型配置不可用") from None
        configured_default = model

    (temporary_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (temporary_home / ".env").write_text("# Disposable text-only Hermes session.\n", encoding="utf-8")
    from hermes_cli.auth import PROVIDER_REGISTRY
    definition = PROVIDER_REGISTRY.get(provider)
    if definition is None or definition.auth_type != "api_key":
        raise _WorkerError("此认证方式暂不支持隔离调用")
    credential_names = definition.api_key_env_vars
    if ark_selected and (tuple(credential_names) != ('VOLCENGINE_API_KEY',)
                         or definition.inference_base_url.rstrip('/') != ARK_BASE_URL
                         or definition.base_url_env_var):
        raise _WorkerError(ROUTE_CHANGED)
    # Parse only this provider's permitted credential/address lines. Do not load
    # unrelated provider keys or evaluate dotenv interpolation.
    env_names = set(credential_names)
    if definition.base_url_env_var:
        env_names.add(definition.base_url_env_var)
    dotenv_path = source_home / '.env'
    try:
        selected_lines = []
        for line in _native_text(dotenv_path, 1024 * 1024, optional=True).splitlines():
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match.group(1) in env_names:
                selected_lines.append(line)
        values = dotenv_values(stream=io.StringIO('\n'.join(selected_lines)), interpolate=False)
    except Exception:
        raise _WorkerError('模型凭据不可用') from None
    for key in credential_names:
        os.environ.pop(key, None)
        value = values.get(key)
        if (isinstance(value, str) and value.strip() and value.isascii()
                and len(value) <= 65536 and not any(char in value for char in ('\r', '\n', '\x00'))):
            os.environ[key] = value.strip()
    if not any(os.environ.get(key) for key in credential_names):
        raise _WorkerError("模型凭据不可用")
    base_url = model_config.get("base_url") or definition.inference_base_url
    if definition.base_url_env_var and values.get(definition.base_url_env_var):
        base_url = values[definition.base_url_env_var]
    if not isinstance(base_url, str) or not base_url.strip():
        raise _WorkerError("模型配置不可用")
    base_url = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                      lambda match: str(values.get(match.group(1)) or ""), base_url)
    from urllib.parse import urlsplit
    url = urlsplit(base_url)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise _WorkerError("模型配置不可用")
    base_url = base_url.rstrip('/')
    safe_model = {"default": model, "configured_default": configured_default,
                  "provider": provider, "base_url": base_url}
    if model == 'deepseek-flash' and _official_base_url(safe_model):
        safe_model['default'] = 'deepseek-v4-flash'
    safe_model['route_fingerprint'] = _snapshot(safe_model)
    if route is not None:
        if (not isinstance(route, dict) or route.get('backend') != 'hermes'
                or route.get('provider') != provider or route.get('base_url') != base_url
                or route.get('default_model') != configured_default
                or route.get('route_fingerprint') != safe_model['route_fingerprint']
                or not valid_model(route.get('model'))
                or route.get('model_id') != identifier('hermes',
                    ('volcengine/' if ark_selected else '') + route['model'])
                or ark_selected and route.get('api_mode') != 'chat_completions'):
            raise _WorkerError(ROUTE_CHANGED)
        safe_model['default'] = route['model']
    elif selected_model is not None and not ark_selected:
        safe_model['default'] = selected_model
    safe_model['model_key'] = ('volcengine/' if ark_selected else '') + safe_model['default']
    if ark_selected:
        safe_model['api_mode'] = 'chat_completions'
    safe_model.update(_image_capability(safe_model))
    safe_model.update(_file_capability(safe_model))
    if images and (safe_model['image_input'] != 'supported' or not isinstance(route, dict)
                   or route.get('image_input') != 'supported'):
        raise _WorkerError(IMAGE_UNSUPPORTED)
    safe_config = {
        "model": {key: safe_model[key] for key in ('default', 'provider', 'base_url')},
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "provider": ""},
        "agent": {"environment_probe": False, "api_max_retries": 1},
        "compression": {"enabled": False},
        "auxiliary": {"background_review": {"enabled": False}},
        "display": {"personality": "none"},
        "mcp_servers": {}, "plugins": {"enabled": []},
    }
    if images:
        # Only this disposable config declares the exact official wire model.
        safe_config['providers'] = {provider: {'models': {
            safe_model['default']: {'supports_vision': True}}}}
    # This copy has no keys, history or user instructions. Credentials stay in process memory.
    (temporary_home / "config.yaml").write_text(yaml.safe_dump(safe_config), encoding="utf-8")
    for filename in ("config.yaml", ".env"):
        (temporary_home / filename).chmod(0o600)
    return safe_model


def _official_base_url(model):
    """The credentialed catalog has one explicit origin; malformed ports fail closed."""
    from urllib.parse import urlsplit
    try:
        url = urlsplit(model['base_url'])
        return (model['provider'] == 'deepseek' and url.scheme == 'https'
                and url.hostname == 'api.deepseek.com' and url.port in (None, 443)
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path.rstrip('/') in ('', '/v1'))
    except (KeyError, TypeError, ValueError):
        return False


def _image_capability(model, selected=None):
    if model.get('provider') == 'volcengine':
        return image_capability('unsupported', '当前方舟文字连接尚未接入原生图片隔离；模型官方能力另记')
    if _official_base_url(model):
        selected = selected or model.get('default') or model.get('configured_default')
        source = 'DeepSeek 官方 Vision/模型表（2026-10-11）的能力声明；推理验收结果另记'
        if selected in ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'):
            return image_capability('supported', source)
        if selected == 'deepseek-v4-pro':
            return image_capability('unsupported', source)
    return image_capability(source='当前提供商与精确请求模型的图像能力未确认')


def _file_capability(model):
    if model.get('provider') == 'volcengine':
        return file_capability('unsupported', '当前方舟连接只接入文字；文件工具尚未接入')
    return file_capability()


def _guard_native_images(agent, model, temporary_home):
    """No native capability probes or auxiliary vision fallbacks in this worker."""
    import yaml
    from agent import image_routing
    config = yaml.safe_load((temporary_home / 'config.yaml').read_text(encoding='utf-8'))
    if (getattr(agent, 'provider', None) != model['provider']
            or str(getattr(agent, 'base_url', '')).rstrip('/') != model['base_url']
            or not callable(getattr(agent, '_model_supports_vision', None))
            or not callable(getattr(agent, '_prepare_messages_for_non_vision_model', None))
            or not callable(getattr(agent, '_describe_image_for_anthropic_fallback', None))
            or image_routing._supports_vision_override(config, agent.provider, agent.model,
                requested_provider=model['provider']) is not True):
        raise _WorkerError(IMAGE_ISOLATION_ERROR)

    def reject_fallback(*args, **kwargs):
        raise _WorkerError(IMAGE_ISOLATION_ERROR)

    image_routing._VISION_PROBES = ()
    agent._describe_image_for_anthropic_fallback = reject_fallback
    if agent._model_supports_vision() is not True:
        raise _WorkerError(IMAGE_ISOLATION_ERROR)


def _official_directory(model):
    """Bounded, redirect-free GET; only the already selected DeepSeek key."""
    import urllib.request
    if not _official_base_url(model):
        return []
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        return []

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        request = urllib.request.Request(model['base_url'] + '/models',
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return []
        rows = json.loads(raw).get('data', [])
        return rows[:200] if isinstance(rows, list) else []
    except Exception:
        # Directory failures are not connector failures; default remains a
        # candidate. Never expose response bodies or silently switch provider.
        return []


def _catalog_entries(model, state, rows):
    route = {'default_model': model['configured_default'], 'base_url': model['base_url'],
             'route_fingerprint': model['route_fingerprint'], **_file_capability(model)}
    result, seen = [], set()
    effective_default = model.get('default') or model['configured_default']
    for row in rows:
        if not isinstance(row, dict):
            continue
        canonical = row.get('id')
        if not valid_model(canonical) or ('input_modalities' in row and 'text' not in row['input_modalities']):
            continue
        # The installed Hermes normalizer rewrites the new flash ID to the old
        # request alias. DeepSeek's official pricing/alias page expressly accepts
        # that alias and serves V4.1 Flash. Keep the wire ID truthful and visible.
        wire = 'deepseek-v4-flash' if canonical == 'deepseek-flash' else canonical
        if wire in seen:
            continue
        seen.add(wire)
        name = row.get('name') or canonical
        if not isinstance(name, str) or len(name) > 120:
            name = canonical
        detail = '当前提供商目录候选；本次请求是否获准由模型服务决定。'
        if wire != canonical:
            detail += ' 本机 Hermes 使用兼容请求别名 ' + wire + '。'
        result.append(entry('hermes', wire, state, name=name, source='当前 DeepSeek 官方 /models',
            status='目录可选', is_default=wire == effective_default, detail=detail,
            route=dict(route, catalog_model=canonical, **_image_capability(model, wire)),
            **_image_capability(model, wire), **_file_capability(model)))
    if effective_default not in seen:
        result.insert(0, entry('hermes', effective_default, state, is_default=True,
            detail='沿用本机默认模型；当前目录未确认该项。',
            route=dict(route, **_image_capability(model, effective_default)),
            **_image_capability(model, effective_default), **_file_capability(model)))
    return result


def _ark_catalog_entries(model):
    """No network discovery or guessed account access for documentary models."""
    state = {'available': True, 'provider': 'volcengine'}
    route = {'default_model': '', 'base_url': model['base_url'],
             'route_fingerprint': model['route_fingerprint'], 'api_mode': 'chat_completions'}
    return [entry('hermes', wire, state, model_key='volcengine/' + wire, name=name,
        source=ARK_SOURCE, status='文档候选',
        detail='官方文档支持文本 Chat API；账户资格由发送时验证。沿用 Hermes 方舟插件默认：关闭深度思考。',
        route=dict(route, **_file_capability(model)), **_image_capability(model, wire),
        **_file_capability(model)) for wire, name in ARK_MODELS]


def _worker() -> int:
    protocol = sys.stdout

    def emit(event: dict) -> None:
        protocol.write(json.dumps(event, ensure_ascii=False) + "\n")
        protocol.flush()

    agent = None
    try:
        payload = json.loads(sys.stdin.read())
        installation = Path(payload["installation"])
        temporary_home = Path(os.environ["HERMES_HOME"])
        sys.path.insert(0, str(installation))
        # Keep all Hermes diagnostics off the JSON/text channel. The parent discards stderr.
        with contextlib.redirect_stdout(sys.stderr):
            snapshots = read_images(payload.get('images'))
            source_home = Path(payload['source_home'])
            model = _worker_model(source_home, temporary_home, payload.get('route'), images=snapshots,
                selected_model=payload.get('selected_model') if payload.get('capture') else None)
            if payload.get("probe"):
                state = {
                    "available": True, "reason": "本机 Hermes 文本连接可用",
                    "provider": model["provider"], "model": model["default"],
                    "detail": "工具和自动记忆关闭；每轮使用共同对话上下文。"}
                if payload.get('capture'):
                    result = {'backend': 'hermes', 'model_id': identifier('hermes', model['model_key']),
                        'model': model['default'], 'provider': model['provider'],
                        'default_model': model['configured_default'], 'base_url': model['base_url'],
                        'route_fingerprint': model['route_fingerprint'], **_image_capability(model),
                        **_file_capability(model)}
                    if model.get('api_mode'):
                        result['api_mode'] = model['api_mode']
                elif payload.get('catalog'):
                    result = {'models': _catalog_entries(model, state, _official_directory(model))}
                    try:
                        ark = _worker_model(source_home, temporary_home,
                            selected_model='volcengine/' + ARK_MODELS[0][0])
                        result['models'].extend(_ark_catalog_entries(ark))
                    except _WorkerError:
                        # A missing additional credential does not change the
                        # native default or turn an unavailable candidate on.
                        pass
                else:
                    result = state
                emit({"type": "result", "result": result})
                return 0

            # Prevent run_agent's legacy installation .env fallback from touching the
            # real home or loading unrelated credentials. This changes only this worker.
            import hermes_cli.env_loader as env_loader
            original_loader = env_loader.load_hermes_dotenv

            def isolated_loader(**kwargs):
                return original_loader(hermes_home=temporary_home, project_env=None,
                                       load_external_secrets=False)

            env_loader.load_hermes_dotenv = isolated_loader
            from hermes_cli.runtime_provider import resolve_runtime_provider
            if snapshots:
                # Before AIAgent initialization, keep cold catalog/local probes off.
                from agent import image_routing
                image_routing._VISION_PROBES = ()
            from run_agent import AIAgent
            try:
                runtime = resolve_runtime_provider(requested=model["provider"],
                                                   explicit_base_url=model["base_url"],
                                                   target_model=model["default"])
                expected_mode = model.get('api_mode') or ('chat_completions' if snapshots else None)
                if (runtime.get('provider') != model['provider']
                        or str(runtime.get('base_url', '')).rstrip('/') != model['base_url']
                        or expected_mode is not None and runtime.get('api_mode') != expected_mode):
                    raise _WorkerError(IMAGE_ISOLATION_ERROR if snapshots else TEXT_ISOLATION_ERROR)
                snapshot = ""

                def on_delta(delta):
                    nonlocal snapshot
                    if isinstance(delta, str) and delta:
                        snapshot += delta
                        emit({"type": "text", "text": snapshot})

                agent = AIAgent(
                    model=model["default"], api_key=runtime.get("api_key"),
                    base_url=runtime.get("base_url"), provider=runtime.get("provider"),
                    requested_provider=model["provider"], api_mode=runtime.get("api_mode"),
                    enabled_toolsets=[], max_iterations=2, run_budget_seconds=180,
                    quiet_mode=True, tool_progress_mode="off", save_trajectories=False,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    checkpoints_enabled=False, stream_delta_callback=on_delta,
                    ephemeral_system_prompt="你是统一聊天入口中的 Hermes 文本助手。根据用户提供的共同对话上下文回答。",
                )
                agent.suppress_status_output = True
                # Avoid lazy session-db acquisition. Even accidental persistence would
                # otherwise go only into the private disposable HERMES_HOME.
                agent._persist_disabled = True
                if agent.tools or agent._memory_manager is not None:
                    raise _WorkerError("Hermes 工具隔离验证失败")
                if agent.model != model['default']:
                    raise _WorkerError('Hermes 的请求模型被本机归一化改变，请刷新后重新选择。')
                if (agent.provider != model['provider']
                        or str(agent.base_url).rstrip('/') != model['base_url']
                        or getattr(agent, 'api_mode', runtime['api_mode']) != runtime['api_mode']):
                    raise _WorkerError(IMAGE_ISOLATION_ERROR if snapshots else TEXT_ISOLATION_ERROR)
                if snapshots:
                    _guard_native_images(agent, model, temporary_home)
            except _WorkerError:
                raise
            except Exception:
                raise _WorkerError("Hermes API 初始化失败") from None
            emit({"type": "status", "text": "Hermes 正在回答…"})
            try:
                content = payload['prompt']
                if snapshots:
                    content = [{'type': 'text', 'text': content}] + [
                        {'type': 'image_url', 'image_url': {'url': 'data:' + image['mime'] + ';base64,' +
                            base64.b64encode(image['data']).decode('ascii')}} for image in snapshots]
                response = agent.run_conversation(user_message=content, conversation_history=[])
            except _WorkerError:
                raise
            except Exception as error:
                raise _WorkerError(_request_failure_reason(error)) from None
            final_text = response.get("final_response") if isinstance(response, dict) else None
            if not isinstance(final_text, str) or not final_text.strip():
                raise _WorkerError("Hermes 未返回完整回答")
            if response.get("failed") or response.get("partial") or response.get("interrupted"):
                raise _WorkerError(_request_failure_reason(response.get("error", "")))
            emit({"type": "result", "result": {
                "text": final_text, "native_session_id": agent.session_id,
                "model": agent.model, "provider": model["provider"]}})
            return 0
    except (_WorkerError, ImageInputError) as error:
        emit({"type": "error", "reason": str(error)})
        return 1
    except Exception:
        emit({"type": "error", "reason": "Hermes 调用失败。"})
        return 1
    finally:
        if agent is not None:
            with contextlib.redirect_stdout(sys.stderr), contextlib.suppress(Exception):
                agent.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        raise SystemExit(_worker())
    print(json.dumps(available(), ensure_ascii=False))
