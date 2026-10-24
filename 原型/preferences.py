"""Private local presentation preferences and honest, read-only adapter inventory.

No model request, credential read, device scan or chat-database mutation occurs
here. The caller supplies the availability results from its existing adapters.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
from agent_registry import IDS, LABELS, valid_model_id, empty_models


MAX_BYTES = 65_536
MAX_FAVORITES = 64
MAX_CHAT_BACKENDS = 512
BACKENDS = frozenset(IDS)
_LEGACY_MODEL_KEYS = frozenset(('codex', 'hermes'))  # Previous persisted format, not current identities.
DEFAULTS = {
    "theme": "system", "font_size": "normal", "density": "comfortable",
    "send_shortcut": "enter", "default_backend": "codex",
    "show_internal": False, "markdown": True, "favorites": [],
    "chat_backends": {}, "default_models": empty_models(),
    "chat_models": {}, "revision": "0",
}
_ENUMS = {
    "theme": {"system", "light", "dark"},
    "font_size": {"normal", "large"},
    "density": {"comfortable", "compact"},
    "send_shortcut": {"enter", "ctrl-enter"},
    "default_backend": BACKENDS,
}
_UPDATE_KEYS = set(DEFAULTS) - {"chat_backends", "chat_models", "revision"}
_CHAT_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_REVISION = re.compile(r"(?:0|[a-f0-9]{32})\Z")
_UNSET = object()


class PreferencesConflict(ValueError):
    """The locked current document, before any write; callers must project it."""
    def __init__(self, current):
        self.current = copy.deepcopy(current)
        super().__init__('设置已由其他入口更新；请读取当前版本后再修改。')


def preference_option(key):
    """Finite scalar option metadata from the same definitions as validation."""
    if key in _ENUMS:
        values, kind = sorted(_ENUMS[key]), 'string'
    elif key in {'show_internal', 'markdown'}:
        values, kind = [False, True], 'boolean'
    else:
        raise ValueError('这个设置没有有限选项目录。')
    return {'type': kind, 'allowed_values': values, 'default': copy.deepcopy(DEFAULTS[key])}


def validate_model_id(model_id, backend=None):
    if not valid_model_id(model_id):
        raise ValueError("模型编号无效。")
    if model_id and backend and not model_id.startswith("local/" + backend + "/"):
        raise ValueError("这个模型不属于所选助手。")
    return model_id


def _validate_value(key, value):
    if key in _ENUMS:
        if not isinstance(value, str) or value not in _ENUMS[key]:
            raise ValueError("设置选项无效：" + key)
    elif key in {"show_internal", "markdown"}:
        if type(value) is not bool:
            raise ValueError("设置开关必须是布尔值：" + key)
    elif key == "favorites":
        if not isinstance(value, list) or len(value) > MAX_FAVORITES:
            raise ValueError("收藏模型列表无效或过长。")
        if any(not item or not valid_model_id(item) for item in value):
            raise ValueError("收藏模型编号无效。")
        if len(set(value)) != len(value):
            raise ValueError("收藏模型不能重复。")
    elif key == "default_models":
        if not isinstance(value, dict) or set(value) != BACKENDS:
            raise ValueError("默认模型选择无效。")
        for backend, model_id in value.items():
            validate_model_id(model_id, backend)


def _validate_document(value):
    # Upgrade only known older schemas; arbitrary missing/extra keys remain an
    # error. Reading an old document never rewrites its original bytes.
    legacy = set(DEFAULTS) - {"default_models", "chat_models"}
    if isinstance(value, dict) and set(value) == legacy:
        value = dict(value, default_models=copy.deepcopy(DEFAULTS["default_models"]), chat_models={})
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("本地设置文件格式不正确；原文件已保留。")
    models = value.get('default_models')
    if isinstance(models, dict) and set(models) == _LEGACY_MODEL_KEYS and set(models) < BACKENDS:
        # Registered identities may grow. Normalize only this existing field
        # in memory; a read never rewrites the user's preference file.
        value = dict(value, default_models=dict(empty_models(), **models))
    for key in _UPDATE_KEYS:
        _validate_value(key, value[key])
    mappings = value["chat_backends"]
    if not isinstance(mappings, dict) or len(mappings) > MAX_CHAT_BACKENDS:
        raise ValueError("本地设置里的聊天选择无效；原文件已保留。")
    for chat_id, backend in mappings.items():
        if not isinstance(chat_id, str) or not _CHAT_ID.fullmatch(chat_id):
            raise ValueError("本地设置里的聊天编号无效；原文件已保留。")
        if not isinstance(backend, str) or backend not in BACKENDS:
            raise ValueError("本地设置里的助手选择无效；原文件已保留。")
    models = value["chat_models"]
    if not isinstance(models, dict) or len(models) > MAX_CHAT_BACKENDS:
        raise ValueError("本地设置里的聊天模型选择无效；原文件已保留。")
    for chat_id, model_id in models.items():
        if not isinstance(chat_id, str) or not _CHAT_ID.fullmatch(chat_id):
            raise ValueError("本地设置里的聊天编号无效；原文件已保留。")
        validate_model_id(model_id, mappings.get(chat_id))
    revision = value["revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError("本地设置版本无效；原文件已保留。")
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("设置文件包含重复键。")
        value[key] = item
    return value


class Preferences:
    """Bounded JSON, no-follow paths, process-safe read/modify/atomic replacement.

    The existing data directory must be canonical and already created by the
    service. Rejecting symbolic-link components keeps reads and replacements
    inside that directory, including when a damaged file is encountered.
    """

    def __init__(self, path):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        if self.path.name in {"", ".", ".."}:
            raise ValueError("设置文件路径无效。")
        self.lock_name = "." + self.path.name + ".lock"

    def _directory_fd(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(os.sep, flags)
        try:
            for part in self.path.parent.parts[1:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OSError:
            os.close(descriptor)
            raise ValueError("设置目录不可用或包含符号链接。") from None

    @contextmanager
    def _locked(self):
        directory = self._directory_fd()
        lock = None
        try:
            # Concurrent first openat(O_CREAT) can fail with ENOENT on macOS.
            # Create once exclusively; later contenders open that same anchor.
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                lock = os.open(self.lock_name, flags | os.O_CREAT | os.O_EXCL,
                               0o600, dir_fd=directory)
            except FileExistsError:
                lock = os.open(self.lock_name, flags, dir_fd=directory)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("设置锁文件无效。")
            os.fchmod(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        except OSError:
            raise ValueError("本地设置暂时无法读取或保存；原文件已保留。") from None
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, directory):
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return copy.deepcopy(DEFAULTS)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > MAX_BYTES):
                raise ValueError("本地设置文件无效或过大；原文件已保留。")
            chunks, count = [], 0
            while True:
                chunk = os.read(descriptor, min(8192, MAX_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > MAX_BYTES:
                    raise ValueError("本地设置文件过大；原文件已保留。")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("本地设置文件无法解析；原文件已保留。") from None
        return _validate_document(value)

    def read(self):
        with self._locked() as directory:
            return self._read(directory)

    def _write(self, directory, value):
        raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("设置内容过长，未保存。")
        name = "." + self.path.name + "." + secrets.token_hex(12) + ".tmp"
        descriptor = None
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Replacing a file name does not follow its target. Even an external
            # race cannot overwrite a symbolic link's destination.
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def update(self, patch, *, expected_revision=_UNSET):
        if not isinstance(patch, dict) or not set(patch).issubset(_UPDATE_KEYS):
            raise ValueError("设置包含不支持的选项。")
        if expected_revision is not _UNSET and (type(expected_revision) is not str
                or not _REVISION.fullmatch(expected_revision)):
            raise ValueError('设置版本无效。')
        for key, value in patch.items():
            _validate_value(key, value)
        with self._locked() as directory:
            value = self._read(directory)
            if expected_revision is not _UNSET and value['revision'] != expected_revision:
                raise PreferencesConflict(value)
            if any(value[key] != item for key, item in patch.items()):
                value.update(copy.deepcopy(patch))
                value["revision"] = secrets.token_hex(16)
                self._write(directory, value)
            return value

    def set_chat_backend(self, chatid, backend, model_id=None):
        if not isinstance(chatid, str) or not _CHAT_ID.fullmatch(chatid):
            raise ValueError("聊天编号无效。")
        if not isinstance(backend, str) or backend not in BACKENDS:
            raise ValueError("助手选择无效。")
        if model_id is not None:
            validate_model_id(model_id, backend)
        with self._locked() as directory:
            value = self._read(directory)
            choices = value["chat_backends"]
            models = value["chat_models"]
            chosen = (models.get(chatid, "") if choices.get(chatid) == backend else "") if model_id is None else model_id
            if choices.get(chatid) != backend or models.get(chatid, "") != chosen:
                if chatid not in choices and len(choices) >= MAX_CHAT_BACKENDS:
                    raise ValueError("保存的聊天助手选择已达到上限。")
                choices[chatid] = backend
                models[chatid] = chosen
                value["revision"] = secrets.token_hex(16)
                self._write(directory, value)
            return value


def _public_text(value, limit=192):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value[:limit] if ord(char) >= 32 and ord(char) != 127).strip()


def management(backends):
    """Describe only supplied registered connectors; do not discover devices."""
    if isinstance(backends, dict):
        rows = [dict(state, id=key) for key, state in backends.items() if isinstance(state, dict)]
    elif isinstance(backends, list):
        rows = backends
    else:
        rows = []
    name = _public_text(platform.node(), 120) or "这台电脑"
    device = {"id": "local", "name": name, "platform": platform.system(), "online": True}
    agents, models, seen = [], [], set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or row["id"] not in BACKENDS or row["id"] in seen):
            continue
        agent_id = row["id"]
        seen.add(agent_id)
        available = row.get("available") is True
        model = _public_text(row.get("model"))
        provider = _public_text(row.get("provider"))
        reason = _public_text(row.get("reason"), 300)
        if not reason:
            reason = "本机可调用（安装与已保存连接配置检查）。" if available else "连接配置未通过或接入未就绪。"
        connection = row.get("connection")
        if not isinstance(connection, str) or connection not in {"online", "local", "unknown"}:
            connection = "online" if agent_id == "codex" and provider == "ChatGPT" else "unknown"
        connection_label = {"online": "在线模型服务", "local": "本机模型服务", "unknown": "已配置模型服务（位置未验证）"}[connection]
        check_scope = "仅检查本机 CLI 和已保存登录／模型配置；本次未发送模型请求。"
        agent = {"id": agent_id, "name": LABELS[agent_id],
                 "device_id": "local", "available": available, "model": model or None,
                 "provider": provider or None, "reason": reason, "connection": connection,
                 "connection_label": connection_label, "check_scope": check_scope}
        agents.append(agent)
        if model:
            models.append({"id": "local/" + agent_id + "/" + model,
                           "agent_id": agent_id, "device_id": "local", "name": model,
                           "connection": connection, "connection_label": connection_label,
                           "provider": provider or None, "available": available, "reason": reason})
    return {"device": device, "agents": agents, "models": models,
            "notice": "当前只展示这台电脑已接入的助手；状态来自安装与保存配置检查，模型服务连通性需以实际回答为准。"}
