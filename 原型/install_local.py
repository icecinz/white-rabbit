#!/usr/bin/env python3
"""Deploy and open the private, local-only One AI macOS service.

Only the explicit runtime allowlist is copied. Chat databases and the native
history index are never copied, replaced, or deleted by this installer.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError, HTTPError
from urllib.parse import quote
from urllib.request import Request
from xml.parsers.expat import ExpatError

import native_startup as native_startup_codec


LABEL = "io.local.one-ai"
PORT = 17860
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_DOCUMENT_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_FILES = 200
MAX_PLIST_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 256 * 1024
MAX_MAINTENANCE_BYTES = 64 * 1024
CONTROL_NAMES = ('install.json', 'manage.py', '打开小白兔.command', '打开一起聊.command', 'native_startup.py')
RUNTIME_FILES = (
    "rabbit_ui/__init__.py", "rabbit_ui/resources.py",
    "rabbit_content/__init__.py", "rabbit_preparation/__init__.py", "rabbit_memory/__init__.py",
    "rabbit_runtime/__init__.py", "rabbit_runtime/pi/__init__.py", "rabbit_runtime/dsh/__init__.py", "rabbit_runtime/claude/__init__.py", "rabbit_runtime/hermes/__init__.py", "rabbit_runtime/_version.py", "rabbit_runtime/maintenance.py", "rabbit_runtime/model_observation.py",
    "rabbit_specialist/__init__.py", "rabbit_preparation/material_references.py",
    "rabbit_content/attachments.py", "rabbit_preparation/text_snapshots.py",
    "server.py", "chat_store.py", "rabbit_specialist/status_checks.py", "rabbit_specialist/status_analysis.py", "status_check_service.py", "native_chat_selection.py", "native_memory_selection.py", "rabbit_preparation/context.py", "rabbit_preparation/text_file_context.py", "history_view.py", "library.py", "preferences.py", "app_management.py", "app_settings.py",
    "native_startup.py", "codex_history_adoption.py", "codex_history_metadata.py", "codex_history_service.py", "native_history_service.py", "hermes_history_adoption.py", "hermes_history_guard.py",
    "admission_gate.py", "service_admission.py", "service_maintenance.py", "native_maintenance.py",
    'rabbit_specialist/interface_tasks.py', 'rabbit_specialist/interface_task_coordinator.py', "native_model_management.py", "native_chat_management.py", "native_work_status.py", "external_work.py", "rabbit_runtime/hapi_work.py",
    "native_task_input.py", "native_task_result.py",
    'rabbit_runtime/codex_rpc.py', 'rabbit_runtime/native_rpc_transport.py', 'rabbit_runtime/native_json.py', 'rabbit_runtime/native_session_store.py', 'rabbit_runtime/native_session_lease.py', 'rabbit_runtime/codex_native_session.py',
    'rabbit_runtime/specialist_service.py', 'rabbit_runtime/specialist_tool_status.py', "specialist_http.py", "specialist_memory.py", 'rabbit_specialist/specialist_workspace.py', 'rabbit_specialist/specialist_connection.py', 'rabbit_runtime/codex_connection.py', "native_chats.py", "native_catalogue.py", "native_http_common.py", "rabbit_preparation/handoff.py", "native_profile_intents.py", "native_profile_catalog.py",
    "rabbit_runtime/pi/rpc.py", "rabbit_runtime/pi/connection.py", "pi_native.py", "rabbit_runtime/pi/session_store.py", "rabbit_runtime/pi/session_file.py", "pi_history_adoption.py",
    "rabbit_runtime/pi/profile_catalog.py", "pi_profile_intents.py", "rabbit_runtime/pi/model_capabilities.py", "rabbit_runtime/pi/thinking_levels.mjs",
    "rabbit_runtime/pi/material_protocol.py", "pi_material_inputs.py", "rabbit_runtime/pi/message_view.py",
    "rabbit_runtime/pi/result_tracking.mjs", "rabbit_runtime/pi/tracked_rpc_bootstrap.mjs", "rabbit_runtime/pi/tracked_rpc_pin.json",
    "rabbit_runtime/hermes/profile_catalog.py", "hermes_profile_intents.py", "hermes_profile_application.py", "rabbit_runtime/hermes/session_settings.py", "rabbit_runtime/hermes/image_queue.py", "rabbit_runtime/hermes/native_bootstrap.py", "rabbit_runtime/hermes/result_cache.py",
    "hermes_material_inputs.py", "rabbit_runtime/hermes/message_view.py",
    "hermes_material_cache.py", "hermes_attachment_transactions.py", "hermes_attachment_view.py",
    "rabbit_runtime/claude/rpc.py", "rabbit_runtime/claude/connection.py", "rabbit_runtime/claude/native_worker.py", "claude_native.py", "rabbit_runtime/claude/session_store.py", "rabbit_runtime/claude/session_file.py",
    "rabbit_runtime/claude/material_protocol.py", "claude_material_inputs.py", "claude_material_intents.py", "native_material_intents.py", "rabbit_runtime/claude/message_view.py", "rabbit_runtime/claude/history_source.py",
    "rabbit_runtime/dsh/rpc.py", "rabbit_runtime/dsh/connection.py", "rabbit_runtime/dsh/worker.mjs", "dsh_native.py", "rabbit_runtime/dsh/session_store.py", "dsh_native_interactions.py",
    "dsh_material_protocol.py", "dsh_material_inputs.py", "dsh_material_intents.py",
    "rabbit_runtime/hermes/rpc.py", "rabbit_runtime/hermes/connection.py", "rabbit_runtime/hermes/worker.mjs", "hermes_native.py", "rabbit_runtime/hermes/session_store.py",
    "rabbit_runtime/hermes/vendor/hermes_gateway/json-rpc-gateway.mjs",
    "rabbit_runtime/hermes/vendor/hermes_gateway/LICENSE", "rabbit_runtime/hermes/vendor/hermes_gateway/upstream.json",
    "codex_adapter.py", "hermes_adapter.py", "claude_adapter.py", "rabbit_runtime/process.py",
    "pi_adapter.py", "pi_worker.mjs", "dsh_adapter.py", "dsh_worker.mjs",
    "agent_registry.py", "history_sources.py", "claude_history.py", "claude_history_adoption.py", "pi_history.py", "rabbit_content/history_records.py",
    "dsh_history.py", "dsh_history_worker.mjs", "dsh_history_adoption.py",
    "model_catalog.py", "rabbit_content/materials.py", "image_inputs.py",
    "native_history_materials.py", "native_materials.py",
    "profiles.py", "profile_capabilities.py", "rabbit_memory/memory_profiles.py", "rabbit_memory/memory_management.py", "rabbit_memory/memory_connection_check.py", "rabbit_memory/hindsight_memory.py", "rabbit_memory/write_jobs.py", "memory_context.py", "rabbit_memory/write_runner.py",
    "rabbit_memory/native_memory_bindings.py", "rabbit_memory/native_hindsight_memory.py", "native_memory_intents.py",
    "rabbit_memory/hermes_files_memory.py", "hermes_file_memory.py", "memory_program_setup.py",
    "rabbit_memory/mem0_memory.py", "mem0_memory_worker.py",
    "rabbit_content/file_intent.py", "rabbit_preparation/file_task_context.py", "rabbit_content/file_task_store.py", "rabbit_content/file_task_files.py",
    "file_task_mcp.py", "codex_file_task.py", "hermes_file_task.py", "document_tickets.py",
    "rabbit_content/project_publish_files.py", "rabbit_content/project_publish_store.py",
    "rabbit_content/markdown_chat_document.py", "rabbit_content/markdown_chat_directory.py", "rabbit_content/markdown_chat_registry.py", "native_markdown_source.py", "rabbit_content/history_markdown_reference.py",
    "rabbit_content/markdown_chat_sync.py", "rabbit_content/markdown_sync_files.py",
    "rabbit_ui/assets/index.html", "rabbit_ui/assets/app.js", "rabbit_ui/assets/style.css", "agent_definitions.json", "rabbit_ui/assets/agents.js",
    "rabbit_ui/assets/workspace.js", "rabbit_ui/assets/workspace.css", "rabbit_ui/assets/browser_state.js", "rabbit_ui/assets/history_quotes.js",
    "rabbit_ui/assets/work_status.js", "rabbit_ui/assets/work_status.css",
    "rabbit_ui/assets/manual.md", "rabbit_ui/assets/specialist-guide.md", "rabbit_ui/assets/status_checks.js",
    "rabbit_ui/assets/specialist_context.js", "rabbit_ui/assets/specialist_tools.js", "rabbit_ui/assets/specialist.js", "rabbit_ui/assets/specialist.css", "rabbit_ui/assets/native_chats.js", "rabbit_ui/assets/native_handoff.js", "rabbit_ui/assets/native_adoption.js", "rabbit_ui/assets/native_text_quote.js", "rabbit_ui/assets/native_file_quote.js", "rabbit_ui/assets/handoff_view.js", "rabbit_ui/assets/native_materials.js",
    "rabbit_ui/assets/pi_native_history.js", "rabbit_ui/assets/pi_native.js",
    "rabbit_ui/assets/hermes_native_history.js", "rabbit_ui/assets/hermes_attachment_state.js", "rabbit_ui/assets/hermes_native.js",
    "rabbit_ui/assets/claude_material_draft.js", "rabbit_ui/assets/claude_native.js",
    "rabbit_ui/assets/dsh_material_draft.js", "rabbit_ui/assets/dsh_native.js",
    "rabbit_ui/assets/text_file_download.js", "rabbit_ui/assets/file_tasks.js", "rabbit_ui/assets/file_materials.js", "rabbit_ui/assets/project_publish.js",
    "rabbit_ui/assets/markdown_chats.js", "rabbit_ui/assets/conversation_groups.js",
    "rabbit_ui/assets/markdown-it.min.js", "rabbit_ui/assets/rich_text.js", "rabbit_ui/assets/markdown-it.LICENSE",
    "rabbit_ui/assets/rabbit-app-1024.png", "rabbit_ui/assets/app.webmanifest", "rabbit_ui/assets/materials.js", "rabbit_ui/assets/profiles.js", "rabbit_ui/assets/native_memory.js",
)
TOOL_FILES = ("local_history_mcp.py", "rabbit_management_client.py", "rabbit_management_mcp.py", "hapi_work_connect.py")


class InstallError(RuntimeError):
    pass


class MaintenanceClient:
    """One in-memory maintenance identity; never adopt or replay lost control.

    A POST with an uncertain reply is followed only by a same-identity state
    lookup. There is deliberately no journal: a new controller must refuse an
    already prepared/closing service, rather than inventing a replacement token.
    """
    ROOT = '/api/maintenance'
    IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')

    def __init__(self, state, csrf):
        self.instance_id = state['instance_id']
        self.csrf = csrf
        self.identity = {'instance_id': self.instance_id,
                         'maintenance_id': secrets.token_hex(16),
                         'token': secrets.token_hex(32),
                         'generation': state['generation'] + 1}
        self.confirmed = None
        self.closing_proven = False

    @staticmethod
    def _json(raw):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate')
                result[key] = value
            return result

        def invalid(_value):
            raise ValueError('noninteger')

        try:
            value = json.loads(raw, object_pairs_hook=pairs,
                               parse_constant=invalid, parse_float=invalid)
            if type(value) is not dict:
                raise ValueError('object required')
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise InstallError('维护回应格式无法确认；没有继续替换程序。') from None

    @classmethod
    def _wire(cls, method, path, *, body=None, csrf=None, allow_absent=False):
        # Fixed loopback endpoint, without urllib/proxy environment or redirects.
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
        try:
            try:
                connection.connect()
            except ConnectionRefusedError:
                if allow_absent:
                    return None
                raise
            headers = {'Accept': 'application/json', 'Connection': 'close',
                       'Origin': 'http://127.0.0.1:' + str(PORT)}
            raw = None
            if body is not None:
                raw = json.dumps(body, separators=(',', ':')).encode('utf-8')
                headers.update({'Content-Type': 'application/json', 'X-OneAI-CSRF': csrf})
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            # An old local service may return an HTML 404. This is an explicit
            # unsupported protocol, never evidence that there is no live service.
            if response.status == 404:
                raise InstallError('现有入口尚不支持安全维护协议；未停止服务，也未部署。')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isascii() or not length.isdigit()
                                       or int(length) > MAX_MAINTENANCE_BYTES):
                raise InstallError('维护回应超过范围；没有继续替换程序。')
            data = response.read(MAX_MAINTENANCE_BYTES + 1)
            if len(data) > MAX_MAINTENANCE_BYTES or (length is not None and len(data) != int(length)):
                raise InstallError('维护回应不完整或超过范围；没有继续替换程序。')
            return response.status, cls._json(data)
        except (OSError, ValueError, http.client.HTTPException):
            raise InstallError('无法确认本机维护回应；原服务与数据保留，未强行接管。') from None
        finally:
            connection.close()

    @classmethod
    def _snapshot(cls, value):
        def number(item):
            return type(item) is int and 0 <= item <= (2 ** 53 - 1)

        def identifier(item):
            return type(item) is str and cls.IDENTIFIER.fullmatch(item) is not None

        fields = {'instance_id', 'state', 'generation', 'maintenance_id', 'phase', 'leases', 'busy'}
        if (type(value) is not dict or value.keys() != fields
                or not identifier(value.get('instance_id')) or not number(value.get('generation'))
                or value.get('state') not in ('running', 'preparing', 'prepared', 'closing')
                or value.get('phase') not in (None, 'preparing', 'prepared', 'closing', 'busy', 'probe_failed', 'cancelled')
                or (value.get('maintenance_id') is not None and not identifier(value['maintenance_id']))):
            raise InstallError('维护状态无法确认；没有继续替换程序。')
        leases, busy = value['leases'], value['busy']
        kinds = {'http', 'service', 'native', 'worker', 'memory', 'repair'}
        if (type(leases) is not dict or leases.keys() != {'total', 'active', 'reserved', 'by_kind'}
                or not all(number(leases[key]) for key in ('total', 'active', 'reserved'))
                or type(leases['by_kind']) is not dict or leases['by_kind'].keys() - kinds
                or not all(number(item) for item in leases['by_kind'].values())
                or leases['active'] + leases['reserved'] != leases['total']
                or sum(leases['by_kind'].values()) != leases['total']
                or type(busy) is not dict or len(busy) > 128
                or any(type(key) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key)
                       or not number(item) or item == 0 for key, item in busy.items())):
            raise InstallError('维护活动计数无法确认；没有继续替换程序。')
        return value

    @classmethod
    def probe(cls, layout):
        result = cls._wire('GET', cls.ROOT, allow_absent=True)
        if result is None:
            return None
        status, value = result
        workspace = hashlib.sha256(str(layout.data / 'chat.sqlite').encode('utf-8')).hexdigest()
        if (status != 200 or value.keys() != {'maintenance', 'workspace_id', 'csrf_token'}
                or value.get('workspace_id') != workspace
                or type(value.get('csrf_token')) is not str
                or not 16 <= len(value['csrf_token']) <= 256
                or any(not 33 <= ord(c) <= 126 for c in value['csrf_token'])):
            raise InstallError('端口上的服务身份无法确认；没有停止服务或部署。')
        state = cls._snapshot(value['maintenance'])
        if state['state'] != 'running':
            raise InstallError('入口已由另一维护操作关门；没有接管或重置。需单独核对维护状态。')
        if state['generation'] == 2 ** 53 - 1:
            raise InstallError('维护编号已到上限；没有继续操作。')
        return cls(state, value['csrf_token'])

    def _post(self, action):
        self.confirmed = None  # A previous response cannot prove a later POST's outcome.
        status, value = self._wire('POST', self.ROOT + '/' + action,
                                  body=self.identity, csrf=self.csrf)
        if (status not in (200, 409) or value.keys() - {'maintenance', 'error', 'code'}
                or 'maintenance' not in value):
            raise InstallError('维护控制结果无法确认；没有继续替换程序。')
        state = self._snapshot(value['maintenance'])
        if any(state[key] != self.identity[key] for key in ('instance_id', 'maintenance_id', 'generation')):
            raise InstallError('维护服务或编号已经变化；没有接管新状态。')
        if action == 'state' and status != 200:
            raise InstallError('原维护编号无法核对；没有重发控制命令。')
        self.confirmed = state
        if state['state'] == 'closing':
            self.closing_proven = True
        return status, state

    def _control(self, action):
        try:
            return self._post(action)
        except InstallError:
            # Exactly one read-only lookup; never repeat prepare/commit/cancel.
            try:
                return self._post('state')
            except InstallError:
                raise InstallError('维护结果未知；未重发控制命令，服务保持现状。需单独核对维护状态。') from None

    def prepare(self):
        status, state = self._control('prepare')
        if (status != 200 or state['state'] != 'prepared' or state['phase'] != 'prepared'
                or state['leases']['total'] or state['busy']):
            raise InstallError('入口仍有任务或维护未准备好；没有停止服务或部署。')

    def commit(self):
        status, state = self._control('commit')
        if (status != 200 or state['state'] != 'closing' or state['phase'] != 'closing'
                or state['leases']['total'] or state['busy']):
            raise InstallError('未确认原维护操作进入关闭阶段；没有停止服务或部署。')

    def cancel_if_safe(self):
        """Fresh exact proof is required; never reopen closing/unknown state."""
        if self.closing_proven:
            return False
        try:
            _status, state = self._post('state')
            if state['state'] == 'running':
                return True
            if state['state'] != 'prepared' or state['phase'] != 'prepared':
                return False
            status, state = self._control('cancel')
            return status == 200 and state['state'] == 'running'
        except InstallError:
            return False


class Layout:
    def __init__(self, home=None):
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.data = self.home / ".imcodes" / "one-ai"
        cloud = self.home / "Library" / "Mobile Documents"
        resolved_data = self.data.resolve()
        if resolved_data == cloud or cloud in resolved_data.parents:
            raise InstallError("安装目录需要位于本机，不能放进 iCloud。")
        self.app = self.data / "app"
        self.index = self.data / 'history.sqlite'
        self.legacy_index = self.home / '.imcodes' / 'user-history' / 'history.sqlite'
        resolved_index = self.index.resolve()
        if resolved_index == cloud or cloud in resolved_index.parents:
            raise InstallError("历史索引需要位于本机，不能放进 iCloud。")
        self.logs = self.data / "logs"
        self.backups = self.data / "backups"
        self.plist = self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")
        self.control = self.data / "manage.py"
        self.state = self.data / "install.json"
        self.entry = self.data / "打开小白兔.command"
        self.legacy_entry = self.data / "打开一起聊.command"
        self.documents = self.data / "project-documents"


def private_directory(path):
    if path.is_symlink():
        raise InstallError("安装目标不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def private_file(path, content):
    if path.is_symlink():
        raise InstallError("安装目标不能是符号链接。")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def control_paths(layout):
    """Fixed local controller surface; never derive a target from snapshot JSON."""
    return dict(zip(CONTROL_NAMES, (layout.state, layout.control, layout.entry,
                                   layout.legacy_entry, layout.data / 'native_startup.py')))


def regular_bytes(path, maximum=MAX_CONTROL_BYTES):
    """Read a bounded ordinary file, or return None for an absent path."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise InstallError('控制文件不是可读取的普通文件，原文件保持原样。') from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise InstallError('控制文件不是有界普通文件，原文件保持原样。')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(maximum + 1)
        after = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) > maximum or not stat.S_ISREG(named.st_mode)
                or identity(before) != identity(after) or identity(after) != identity(named)):
            raise InstallError('控制文件在读取时发生变化，暂不继续。')
        return raw
    except OSError:
        raise InstallError('无法确认控制文件，原文件保持原样。') from None
    finally:
        os.close(descriptor)


def read_controls(layout):
    return {name: regular_bytes(path) for name, path in control_paths(layout).items()}


def restore_controls(layout, payloads):
    """Restore only named managed controls, including explicit absence."""
    paths = control_paths(layout)
    if payloads.keys() - paths.keys():
        raise InstallError('恢复清单包含未知控制文件。')
    read_controls(layout)  # Reject unsafe current targets before the first mutation.
    for name, raw in payloads.items():
        target = paths[name]
        if raw is None:
            target.unlink(missing_ok=True)
        else:
            private_file(target, raw)
            if name.endswith('.command'):
                target.chmod(0o700)


def controller_payloads():
    """Freeze the standalone controller and its paired stdlib codec before stop."""
    root = Path(__file__).absolute().parent
    payloads = {'manage.py': regular_bytes(root / Path(__file__).name),
                'native_startup.py': regular_bytes(root / 'native_startup.py')}
    if any(raw is None for raw in payloads.values()):
        raise InstallError('独立控制器或启动配置模块不可用。')
    for name, raw in payloads.items():
        compile(raw, name, 'exec')
    return payloads


def _saved_state(layout):
    raw = regular_bytes(layout.state)
    if raw is None:
        return {}

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise InstallError('已保存的安装状态含重复字段，暂不部署。')
            value[key] = item
        return value

    def constant(_value):
        raise InstallError('已保存的安装状态含无效数值，暂不部署。')

    try:
        saved = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InstallError('已保存的安装状态无法确认，暂不部署。') from None
    if type(saved) is not dict:
        raise InstallError('已保存的安装状态无法确认，暂不部署。')
    return saved


def native_configuration(layout, config_file=None):
    """Explicit JSON overrides; omission preserves the saved finite config."""
    try:
        if config_file is not None:
            # Make CLI-relative input explicit without resolving a symlink.
            return native_startup_codec.load(Path(config_file).expanduser().absolute())
        saved = _saved_state(layout)
        if 'native_startup' not in saved:
            return None  # Proven old/absent state: every native flag stays off.
        return native_startup_codec.normalize(saved['native_startup'])
    except native_startup_codec.NativeStartupError as error:
        raise InstallError(str(error)) from None


def refresh_document_cache(layout, source):
    """Publish a bounded text-only project mirror from the user's entry process.

    Background launchd jobs can be denied access to iCloud while this foreground
    entry can read it. The mirror never scans a home or copies chat databases.
    Immutable generation contents and an atomic manifest swap avoid mixed reads.
    """
    try:
        root = Path(source).expanduser().resolve(strict=True).parent
        if any(part.startswith('.') for part in root.parts) or not root.is_dir():
            raise InstallError("项目文字副本需要一个可读取的普通项目目录。")
    except (OSError, RuntimeError):
        raise InstallError("无法读取原项目目录，已有项目文字副本保持原样。") from None
    private_directory(layout.home / ".imcodes")
    private_directory(layout.data)
    with exclusive_file(layout.data / "project-documents.lock", "项目文字副本正在更新，请稍后再试。"):
        payloads, files = {}, {}
        total = 0

        def scan(directory, prefix=()):
            nonlocal total
            for name in sorted(os.listdir(directory)):
                if len(files) >= MAX_DOCUMENT_FILES:
                    break
                if name.startswith('.'):
                    continue
                parts = prefix + (name,)
                try:
                    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=directory)
                        try:
                            scan(child, parts)
                        finally:
                            os.close(child)
                    elif (stat.S_ISREG(metadata.st_mode)
                          and Path(name).suffix.lower() in ('.md', '.txt')
                          and metadata.st_size <= MAX_DOCUMENT_BYTES):
                        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                             dir_fd=directory)
                        try:
                            actual = os.fstat(descriptor)
                            if not stat.S_ISREG(actual.st_mode) or actual.st_size > MAX_DOCUMENT_BYTES:
                                continue
                            with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                                raw = handle.read(MAX_DOCUMENT_BYTES + 1)
                        finally:
                            os.close(descriptor)
                        if len(raw) > MAX_DOCUMENT_BYTES or total + len(raw) > MAX_DOCUMENT_TOTAL_BYTES:
                            continue
                        try:
                            raw.decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                        relative = '/'.join(parts)
                        key = hashlib.sha256(relative.encode('utf-8')).hexdigest()
                        files[relative] = key
                        payloads[key] = raw
                        total += len(raw)
                except OSError:
                    # Missing, inaccessible, replaced or symlink entries are
                    # simply not eligible for this explicit project snapshot.
                    continue

        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                scan(directory)
            finally:
                os.close(directory)
        except OSError:
            raise InstallError("无法读取原项目目录，已有项目文字副本保持原样。") from None
        private_directory(layout.documents)
        generations = layout.documents / "generations"
        private_directory(generations)
        manifest_path = layout.documents / "manifest.json"
        previous = None
        if manifest_path.is_symlink():
            raise InstallError("项目文字副本清单不能是符号链接。")
        if manifest_path.is_file() and manifest_path.stat().st_size <= 1024 * 1024:
            try:
                old = json.loads(manifest_path.read_text(encoding='utf-8'))
                value = old.get('generation') if isinstance(old, dict) else None
                if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value):
                    previous = value
            except (ValueError, OSError):
                pass
        generation = secrets.token_hex(16)
        updated_at = datetime.now(timezone.utc).isoformat()
        manifest = {'version': 1, 'source': str(root), 'updated_at': updated_at,
                    'generation': generation, 'files': files}
        with tempfile.TemporaryDirectory(prefix='.generation-stage-', dir=generations) as temporary:
            stage = Path(temporary) / 'documents'
            private_directory(stage)
            for key, raw in payloads.items():
                private_file(stage / (key + '.txt'), raw)
            private_file(stage / 'generation.json', json.dumps(
                {'creator': 'one-ai-project-documents', 'generation': generation}).encode())
            os.replace(stage, generations / generation)
        private_file(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        # Keep current and previous only. Unknown names, symlinks and unmarked
        # directories are never removed by this narrowly owned cache cleaner.
        for old_directory in generations.iterdir():
            if (old_directory.name in (generation, previous)
                    or not re.fullmatch(r'[0-9a-f]{32}', old_directory.name)
                    or old_directory.is_symlink() or not old_directory.is_dir()):
                continue
            marker = old_directory / 'generation.json'
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024:
                continue
            try:
                owned = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if owned == {'creator': 'one-ai-project-documents', 'generation': old_directory.name}:
                shutil.rmtree(old_directory)
        return {'files': len(files), 'bytes': total, 'updated_at': updated_at}


def python_executable():
    candidates = (sys.executable, shutil.which("python3"), "/usr/bin/python3")
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise InstallError("没有找到可以运行本机服务的 Python 3。")


def installed_plist_bytes(layout):
    """Read only the bounded ordinary saved job, never a link or FIFO."""
    try:
        descriptor = os.open(layout.plist, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise InstallError("已保存的启动配置不是可读取的普通文件。") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PLIST_BYTES:
            raise InstallError("已保存的启动配置不是有界普通文件。")
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(MAX_PLIST_BYTES + 1)
        after = os.fstat(descriptor)
        named = os.stat(layout.plist, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) > MAX_PLIST_BYTES or identity(before) != identity(after)
                or identity(after) != identity(named)):
            raise InstallError("已保存的启动配置在读取时发生变化，暂不部署。")
        return raw
    except OSError:
        raise InstallError("无法确认已保存的启动配置，暂不部署。") from None
    finally:
        os.close(descriptor)


def installed_path(layout):
    raw = installed_plist_bytes(layout)
    if raw is None:
        return None
    try:
        saved = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException, ExpatError):
        return None
    if not isinstance(saved, dict) or saved.get('Label') != LABEL:
        return None
    arguments = saved.get('ProgramArguments')
    if (not isinstance(arguments, list) or len(arguments) < 3
            or any(not isinstance(argument, str) or '\x00' in argument for argument in arguments)
            or not arguments[0]
            or arguments[1:3] != ['-B', str(layout.app / '原型' / 'server.py')]
            or saved.get('WorkingDirectory') != str(layout.data)):
        return None
    environment = saved.get('EnvironmentVariables')
    value = environment.get('PATH') if isinstance(environment, dict) else None
    if (not isinstance(value, str) or not value or len(value) > MAX_PLIST_BYTES // 2
            or any(ord(character) < 32 or ord(character) == 127
                   or 0xD800 <= ord(character) <= 0xDFFF for character in value)):
        return None
    return value


def plist_payload(layout, python, documents_dir=None, *, publication_dir=None, native_startup=None):
    arguments = [python, "-B", str(layout.app / "原型" / "server.py"),
                 "--port", str(PORT), "--data-dir", str(layout.data),
                 "--index", str(layout.index)]
    if documents_dir is not None:
        arguments.extend(("--documents-dir", str(Path(documents_dir).resolve())))
    if publication_dir is not None:
        arguments.extend(("--publication-dir", str(Path(publication_dir).resolve())))
    if native_startup is not None:
        # install() already checked every local path before any stop/write.
        arguments.extend(native_startup_codec.arguments(native_startup, check_paths=False))
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(layout.data),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 180,
        "EnvironmentVariables": {
            # PATH belongs to this installed job. Updating code or arguments
            # must not replace it with the caller's unrelated shell search path.
            # All other fields below remain freshly generated for this version.
            "PATH": installed_path(layout) or os.environ.get("PATH") or ":".join((str(layout.home / ".local/bin"),
                         "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(layout.logs / "service.log"),
        "StandardErrorPath": str(layout.logs / "error.log"),
        "Umask": 0o077,
    }


def source_files(source):
    source = Path(source).absolute()
    files = {}
    for prefix, root, names in (("原型", source, RUNTIME_FILES),
                                ("工具", source.parent / "工具", TOOL_FILES)):
        for name in names:
            if (not isinstance(name, str) or not name or name != name.strip()
                    or name.startswith('/') or '\\' in name or re.match(r'^[A-Za-z]:', name)
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                    or any(part in ('', '.', '..') for part in name.split('/'))):
                raise InstallError("运行文件清单路径必须是规范的相对路径。")
            relative = prefix + '/' + name
            if relative in files:
                raise InstallError("运行文件清单路径重复：" + relative)
            path = root / name
            # A regular leaf is insufficient if one of its directories redirects
            # outside the declared source. Never follow directory links here.
            for directory in reversed(path.parents):
                if directory.is_symlink() or not directory.is_dir():
                    raise InstallError("运行文件目录不是普通目录：" + relative)
            if path.is_symlink() or not path.is_file():
                raise InstallError("运行文件缺失或不是普通文件：" + relative)
            files[relative] = path
    for relative, path in files.items():
        if path.suffix == ".py":
            # compile() validates without importing the app, probing models or
            # writing __pycache__ into the source project or installed tree.
            compile(path.read_bytes(), str(path), "exec")
    return files


def active_runs(layout):
    database = layout.data / "chat.sqlite"
    if not database.is_file():
        return 0
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.execute("PRAGMA query_only=ON")
            return connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
    except sqlite3.Error:
        raise InstallError("聊天库状态无法确认，暂不部署。原数据库保持原样。") from None


def require_idle(layout):
    if active_runs(layout):
        raise InstallError("有对话正在回答或停止中；请等回答结束后再安装或卸载。")


@contextlib.contextmanager
def exclusive_file(path, reason):
    if path.is_symlink():
        raise InstallError("安装锁不能是符号链接。")
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstallError(reason) from None
        yield
    finally:
        os.close(descriptor)


def launchctl(*arguments, check=True, timeout=20):
    result = subprocess.run(["/bin/launchctl", *arguments], capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode:
        # Do not print arbitrary service diagnostics or private log contents.
        raise InstallError("本机启动服务未完成；可以再次点开入口重试。")
    return result


def job_target():
    return "gui/" + str(os.getuid()) + "/" + LABEL


def job_loaded():
    return launchctl("print", job_target(), check=False).returncode == 0


def stop_managed_job():
    if job_loaded():
        # launchd allows the managed service up to ExitTimeOut=180 to close its
        # owned Agent processes. A controller timeout must not cut that short.
        launchctl("bootout", job_target(), timeout=200)


def ensure_macos():
    if sys.platform != "darwin":
        raise InstallError("启动和卸载入口只支持 macOS。")


def wait_service_unlocked(layout):
    deadline = time.monotonic() + 195
    while True:
        try:
            with exclusive_file(layout.data / "service.lock", "仍在运行"):
                return
        except InstallError:
            if time.monotonic() >= deadline:
                raise InstallError("旧本机入口仍在运行；请先正常停止旧入口，再部署。") from None
            time.sleep(0.1)


@contextlib.contextmanager
def stopped_service(layout, activate_service, *, before_stop=None):
    """Yield only with admission closed (if live) and the service lock owned.

    With no live listener the file lock is still required. A live server must
    prove this process's prepare/commit before bootout; no-activate never stops
    a live service. Lost in-memory control is not recoverable by a new token.

    The caller must retain install.lock EX through this context and activate().
    A new Service takes install.lock SH before service.lock EX, then releases
    SH. This prevents a KeepAlive replacement from accepting work between the
    original instance's commit and launchd bootout. A live Service holds no SH.
    """
    if activate_service:
        ensure_macos()
    client = MaintenanceClient.probe(layout)
    if client is None:
        # A refused TCP connection alone does not prove the server has exited:
        # it may be starting/stopping with its service lock already held.
        with exclusive_file(layout.data / 'service.lock', '旧本机入口仍在运行；没有部署。'):
            require_idle(layout)
            if before_stop is not None:
                before_stop()
            if activate_service:
                stop_managed_job()
            yield
        return
    if not activate_service:
        raise InstallError('入口仍在线；--no-activate 不会停止在线服务，没有部署。')
    try:
        client.prepare()
        require_idle(layout)
        if before_stop is not None:
            before_stop()
        client.commit()
        stop_managed_job()
        wait_service_unlocked(layout)
        with exclusive_file(layout.data / 'service.lock', '旧本机入口仍在收尾；没有部署。'):
            require_idle(layout)
            yield
    except BaseException as error:
        # Recheck before cancelling. A timed-out commit invalidated all earlier
        # prepared proof, and a proven closing service is never reopened here.
        restored = client.cancel_if_safe()
        if isinstance(error, Exception) and not restored:
            raise InstallError('维护尚未完成；未强行接管或自动重新开门。服务保持现状，需单独核对维护状态。') from None
        raise


def snapshot(layout):
    controls = read_controls(layout)
    plist = installed_plist_bytes(layout)
    if layout.app.is_symlink():
        raise InstallError("运行目录不能是符号链接。")
    metadata = {'schema_version': 1, 'app': layout.app.exists(), 'plist': plist is not None,
                'controls': {name: raw is not None for name, raw in controls.items()}}
    private_directory(layout.backups)
    backup = layout.backups / str(time.time_ns())
    if backup.exists() or backup.is_symlink():
        raise InstallError('恢复副本名称已存在，暂不继续。')
    with tempfile.TemporaryDirectory(prefix='.snapshot-stage-', dir=layout.backups) as temporary:
        stage = Path(temporary) / 'snapshot'
        private_directory(stage)
        if metadata['app']:
            shutil.copytree(layout.app, stage / 'app')
        if plist is not None:
            private_file(stage / 'agent.plist', plist)
        private_directory(stage / 'controls')
        for name, raw in controls.items():
            if raw is not None:
                private_file(stage / 'controls' / name, raw)
        private_file(stage / 'snapshot.json', json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
        os.replace(stage, backup)
    return backup


def read_snapshot(backup):
    """Validate one generation completely before touching the installed app."""
    if backup.is_symlink() or (backup / 'app').is_symlink():
        raise InstallError('恢复副本不能使用符号链接。')
    raw = regular_bytes(backup / 'snapshot.json', MAX_PLIST_BYTES)
    try:
        metadata = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError):
        metadata = None
    if (type(metadata) is not dict or type(metadata.get('app')) is not bool
            or type(metadata.get('plist')) is not bool or metadata['app'] is not True
            or not (backup / 'app').is_dir()):
        raise InstallError('恢复副本清单无效。')
    plist = regular_bytes(backup / 'agent.plist', MAX_PLIST_BYTES)
    if (plist is not None) != metadata['plist']:
        raise InstallError('恢复副本的启动配置与清单不一致。')
    if 'schema_version' not in metadata:
        if set(metadata) != {'app', 'plist'}:
            raise InstallError('旧恢复副本清单无效。')
        return {'plist': plist, 'controls': None}
    if (set(metadata) != {'schema_version', 'app', 'plist', 'controls'}
            or type(metadata['schema_version']) is not int or metadata['schema_version'] != 1
            or type(metadata['controls']) is not dict or set(metadata['controls']) != set(CONTROL_NAMES)
            or any(type(value) is not bool for value in metadata['controls'].values())
            or (backup / 'controls').is_symlink() or not (backup / 'controls').is_dir()):
        raise InstallError('恢复副本控制清单无效。')
    controls = {}
    for name in CONTROL_NAMES:
        value = regular_bytes(backup / 'controls' / name)
        if (value is not None) != metadata['controls'][name]:
            raise InstallError('恢复副本控制文件与清单不一致。')
        controls[name] = value
    return {'plist': plist, 'controls': controls}


def legacy_restore_state(layout, plist):
    """Old backups have no controller state: keep only known path fields."""
    raw = regular_bytes(layout.state)
    try:
        current = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError):
        raise InstallError('当前安装状态无法确认，暂不恢复旧副本。') from None
    if type(current) is not dict:
        raise InstallError('当前安装状态无法确认，暂不恢复旧副本。')

    def known_path(value):
        return (type(value) is str and value and len(value) < MAX_CONTROL_BYTES
                and Path(value).is_absolute() and '..' not in Path(value).parts
                and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))

    saved = {key: current[key] for key in ('source', 'python') if known_path(current.get(key))}
    if plist is not None:
        try:
            job = plistlib.loads(plist)
            argv = job.get('ProgramArguments') if type(job) is dict else None
            if (job.get('Label') == LABEL and type(argv) is list and len(argv) >= 3
                    and argv[1:3] == ['-B', str(layout.app / '原型/server.py')]
                    and known_path(argv[0])):
                saved['python'] = argv[0]
        except (ValueError, plistlib.InvalidFileException, ExpatError, AttributeError):
            pass
    return json.dumps(saved, ensure_ascii=False).encode('utf-8')


def default_source(layout):
    if layout.state.is_file():
        try:
            saved = json.loads(layout.state.read_text(encoding="utf-8"))
            if isinstance(saved.get("source"), str):
                return Path(saved["source"])
        except (ValueError, OSError):
            pass
    return Path(__file__).resolve().parent


def prepare_directories(layout):
    private_directory(layout.home / ".imcodes")
    private_directory(layout.data)
    private_directory(layout.logs)
    for name in ("service.log", "error.log"):
        path = layout.logs / name
        if path.is_symlink():
            raise InstallError("日志目标不能是符号链接。")
        descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
    # Never chmod a pre-existing shared LaunchAgents folder.
    layout.plist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def deploy(layout, source, python, *, native_startup=None, controllers=None):
    files = source_files(source)
    manifest = {relative: hashlib.sha256(path.read_bytes()).hexdigest()
                for relative, path in files.items()}
    encoded_manifest = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    # The runtime is copied into a private directory, while links refer to the
    # original project. Keep that explicit root in the saved plist; rollback
    # restores its snapshot plist unchanged, including older no-preview jobs.
    plist = plistlib.dumps(plist_payload(layout, python, source.parent, publication_dir=source.parent,
                                        native_startup=native_startup), sort_keys=True)
    payload_unchanged = (layout.app / "manifest.json").is_file() and (
        (layout.app / "manifest.json").read_bytes() == encoded_manifest
        and installed_plist_bytes(layout) == plist)
    if payload_unchanged:
        payload_unchanged = all((layout.app / relative).is_file()
                        and not (layout.app / relative).is_symlink()
                        and hashlib.sha256((layout.app / relative).read_bytes()).hexdigest() == digest
                        for relative, digest in manifest.items())
    controllers = controller_payloads() if controllers is None else controllers
    entry = ('#!/bin/zsh\nexec ' + shlex.quote(python) + ' -B '
             + shlex.quote(str(layout.control)) + ' open\n').encode('utf-8')
    current = read_controls(layout)
    state = {'source': str(source), 'python': python}
    if native_startup is not None:
        state['native_startup'] = native_startup_codec.normalize(native_startup, check_paths=False)
    desired = {**controllers, 'install.json': json.dumps(state, ensure_ascii=False).encode('utf-8'),
               '打开小白兔.command': entry}
    if current['打开一起聊.command'] is not None:
        desired['打开一起聊.command'] = entry
    paths = control_paths(layout)
    control_unchanged = all(current[name] == raw and stat.S_IMODE(paths[name].stat().st_mode)
                            == (0o700 if name.endswith('.command') else 0o600)
                            for name, raw in desired.items())
    unchanged = payload_unchanged and control_unchanged
    backup = None
    if not unchanged:
        backup = snapshot(layout)
    if not payload_unchanged:
        with tempfile.TemporaryDirectory(prefix=".app-stage-", dir=layout.data) as name:
            stage = Path(name) / "app"
            private_directory(stage)
            for relative, original in files.items():
                destination = stage / relative
                directory = stage
                for part in Path(relative).parent.parts:
                    directory = directory / part
                    private_directory(directory)
                private_file(destination, original.read_bytes())
            private_file(stage / "manifest.json", encoded_manifest)
            if layout.app.exists():
                shutil.rmtree(layout.app)
            os.replace(stage, layout.app)
            private_file(layout.plist, plist)
    for name, raw in desired.items():
        mode = 0o700 if name.endswith('.command') else 0o600
        if current[name] != raw or stat.S_IMODE(paths[name].stat().st_mode) != mode:
            private_file(paths[name], raw)
            paths[name].chmod(mode)
    return {"changed": not unchanged, "backup": str(backup) if backup else None,
            "app": str(layout.app), "entry": str(layout.entry), "plist": str(layout.plist)}


def activate(layout):
    ensure_macos()
    if not layout.plist.is_file():
        raise InstallError("本机入口尚未安装。")
    if job_loaded():
        # A job that is still starting can report "already running". Never use
        # -k here: opening the page must not interrupt an existing answer.
        launchctl("kickstart", job_target(), check=False)
    else:
        launchctl("bootstrap", "gui/" + str(os.getuid()), str(layout.plist))


def install(layout, source=None, activate_service=True, *, native_config=None):
    # source/defaults and native settings must describe the same saved generation.
    # Capture absence too; another installer may finish before this one takes its lock.
    initial_state = regular_bytes(layout.state)
    source = Path(source or default_source(layout)).expanduser().resolve()
    source_files(source)  # Validate all inputs before stopping a managed job.
    native_startup = native_configuration(layout, native_config)
    controllers = controller_payloads()
    python = python_executable()
    prepare_directories(layout)
    with exclusive_file(layout.data / "install.lock", "另一个安装操作正在进行。"):
        if regular_bytes(layout.state) != initial_state:
            raise InstallError('安装状态已变化；没有停止服务或部署，请重新核对后再试。')
        with stopped_service(layout, activate_service,
                             before_stop=lambda: refresh_document_cache(layout, source)):
            result = deploy(layout, source, python, native_startup=native_startup, controllers=controllers)
        if activate_service:
            # Keep EX during bootstrap: the new Service waits at its SH startup
            # fence until this controller exits the installation critical section.
            activate(layout)
    result['native_agents'] = [name for name in (native_startup or {}).get('connections', {})
                              if name != 'specialist']
    return result


def check_installation(layout, source=None, *, native_config=None):
    """Read installation inputs only; never launch a service or inspect credentials."""
    try:
        ensure_macos()
        macos = True
    except InstallError:
        macos = False
    python_supported = sys.version_info >= (3, 11)
    report = {
        'schema_version': 1,
        'environment': {'macos_supported': macos, 'python_supported': python_supported,
                        'python_version': '.'.join(str(value) for value in sys.version_info[:3])},
        'source': {'valid': False},
        'native_configuration': {'source': 'explicit' if native_config is not None else 'none',
                                 'valid': False, 'agents': [], 'specialist_enabled': False},
        'installation': {'metadata_present': layout.state.is_file(),
                         'application_present': (layout.app / '原型/server.py').is_file(),
                         'entry_present': layout.entry.is_file(),
                         'data_dir': str(layout.data), 'entry': str(layout.entry)},
        'url': 'http://127.0.0.1:' + str(PORT) + '/',
        'visible_commands': {name: shutil.which(name) for name in
                             ('python3', 'node', 'codex', 'hermes', 'pi', 'claude', 'dsh')},
        'authentication': 'not_checked', 'real_chat': 'not_checked', 'service': 'not_checked',
    }
    try:
        chosen_source = source
        if chosen_source is None:
            saved_source = _saved_state(layout).get('source')
            chosen_source = saved_source if isinstance(saved_source, str) else Path(__file__).absolute().parent
        root = Path(chosen_source).expanduser().resolve()
        files = source_files(root)
        report['source'] = {'valid': True, 'path': str(root), 'files': len(files)}
    except (InstallError, OSError, SyntaxError, ValueError) as error:
        report['source']['error'] = str(error)
    try:
        if native_config is None and 'native_startup' in _saved_state(layout):
            report['native_configuration']['source'] = 'saved'
        value = native_configuration(layout, native_config)
        connections = (value or {}).get('connections', {})
        report['native_configuration'].update(
            valid=True, agents=[name for name in connections if name != 'specialist'],
            specialist_enabled='specialist' in connections)
    except (InstallError, OSError, ValueError) as error:
        report['native_configuration']['error'] = str(error)
    if not macos or not python_supported:
        report['status'] = 'unsupported_environment'
    elif not report['source']['valid']:
        report['status'] = 'invalid_source'
    elif not report['native_configuration']['valid']:
        report['status'] = 'invalid_configuration'
    elif not report['native_configuration']['agents']:
        report['status'] = 'needs_configuration'
    else:
        report['status'] = 'ready_for_installation'
    return report


def local_status():
    # Ignore proxy settings for the loopback request.
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = Request("http://127.0.0.1:" + str(PORT) + "/api/status")
    try:
        with opener.open(request, timeout=1) as response:
            value = json.loads(response.read(1024 * 1024))
        if isinstance(value, dict) and value.get("local_only") is True and value.get("version"):
            return value
        raise InstallError("本机入口端口已被其他服务占用。")
    except (HTTPError, ValueError):
        raise InstallError("本机入口端口已被其他服务占用或返回了无效页面。") from None
    except (URLError, OSError):
        return None


def open_entry(layout):
    ensure_macos()
    try:
        refresh_document_cache(layout, default_source(layout))
    except (InstallError, OSError):
        # Opening an existing chat stays usable if its original project has
        # moved or the foreground entry cannot refresh the bounded mirror.
        print("项目文字副本未更新；已有副本保留，聊天入口仍可打开。", file=sys.stderr)
    if local_status() is None:
        if not (layout.app / "原型" / "server.py").is_file() or not layout.plist.is_file():
            install(layout, activate_service=False)
        activate(layout)
        deadline = time.monotonic() + 90
        while local_status() is None:
            if time.monotonic() >= deadline:
                raise InstallError("本机入口尚未就绪；请稍后再次点开。聊天数据已保留。")
            time.sleep(0.3)
    subprocess.run(["/usr/bin/open", "http://127.0.0.1:" + str(PORT)], check=True)


def uninstall(layout, keep_data=True):
    ensure_macos()
    prepare_directories(layout)
    with exclusive_file(layout.data / "install.lock", "另一个安装操作正在进行。"):
        with stopped_service(layout, True):
            backup = snapshot(layout)
            if layout.app.exists():
                shutil.rmtree(layout.app)
            layout.plist.unlink(missing_ok=True)
    # The private control/desktop entry and snapshots remain usable for rollback.
    # Even without the flag, this tool never deletes chat.sqlite or history.sqlite.
    return {"backup": str(backup), "data_kept": True}


def rollback(layout, activate_service=True):
    prepare_directories(layout)
    backups = sorted(layout.backups.glob("*/snapshot.json"), reverse=True)
    choices = [item.parent for item in backups if (item.parent / "app").is_dir()]
    if not choices:
        raise InstallError("没有可恢复的旧运行版本。")
    chosen = choices[0]
    payload = read_snapshot(chosen)
    # Resolve the legacy fallback before stopping a service or replacing its app.
    legacy_state = legacy_restore_state(layout, payload['plist']) if payload['controls'] is None else None
    read_controls(layout)
    with exclusive_file(layout.data / "install.lock", "另一个安装操作正在进行。"):
        with stopped_service(layout, activate_service):
            snapshot(layout)
            if layout.app.exists():
                shutil.rmtree(layout.app)
            shutil.copytree(chosen / "app", layout.app)
            if payload['plist'] is not None:
                private_file(layout.plist, payload['plist'])
            else:
                private_file(layout.plist, plistlib.dumps(plist_payload(layout, python_executable())))
            if payload['controls'] is not None:
                restore_controls(layout, payload['controls'])
            else:
                # The old generation cannot prove controller/helper bytes.
                # Retain the working standalone controller, but carry no new
                # native startup state into the legacy app.
                private_file(layout.state, legacy_state)
        if activate_service:
            # The restored Service follows the same SH startup fence as install.
            activate(layout)
    return {"restored_from": str(chosen), "data_kept": True}


def main():
    parser = argparse.ArgumentParser(description="小白兔：本机安装、打开与保留数据卸载")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser('check', help='只读检查环境、源码及原生启动配置；不启动Agent')
    check_parser.add_argument('--source-dir', type=Path, help='原型源代码目录')
    check_parser.add_argument('--native-config', type=Path, help='仅核对路径的有限原生启动JSON')
    install_parser = commands.add_parser("install", help="安装私有运行副本与登录启动入口")
    install_parser.add_argument("--no-activate", action="store_true", help="只部署，不启动")
    install_parser.add_argument("--source-dir", type=Path, help="原型源代码目录")
    install_parser.add_argument('--native-config', type=Path,
                                help='显式有限原生启动JSON；省略时保留已保存配置，空connections关闭全部入口')
    commands.add_parser("open", help="启动已安装入口并打开本机页面")
    uninstall_parser = commands.add_parser("uninstall", help="停用入口；保留数据和恢复副本")
    uninstall_parser.add_argument("--keep-data", action="store_true", help="保留数据（默认一直保留）")
    rollback_parser = commands.add_parser("rollback", help="恢复最近运行版本；保留数据")
    rollback_parser.add_argument("--no-activate", action="store_true", help="恢复后不启动")
    args = parser.parse_args()
    if args.command == 'check':
        try:
            result = check_installation(Layout(), args.source_dir, native_config=args.native_config)
        except (InstallError, OSError, ValueError) as error:
            result = {'schema_version': 1, 'status': 'check_failed', 'error': str(error),
                      'authentication': 'not_checked', 'real_chat': 'not_checked'}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'ready_for_installation' else 1
    if sys.version_info < (3, 11):
        print('需要 Python 3.11 或更新版本；尚未部署或启动。', file=sys.stderr)
        return 1
    os.umask(0o077)
    layout = Layout()
    try:
        if args.command == "install":
            result = install(layout, args.source_dir, not args.no_activate, native_config=args.native_config)
            print("本机入口已部署：" + result["entry"])
            print("现有聊天和历史数据已保留。")
            agents = result.get('native_agents')
            if agents is not None:
                print('已配置的原生入口：' + ('、'.join(agents) if agents else '尚未配置'))
            print('部署完成不等于真实聊天验收；请完成一次真实发送、刷新和续问。')
            print('下次打开：' + result['entry'])
        elif args.command == "open":
            open_entry(layout)
        elif args.command == "uninstall":
            uninstall(layout)
            print("入口已停用，聊天和历史数据已保留。可使用 rollback 恢复。")
        elif args.command == "rollback":
            rollback(layout, not args.no_activate)
            print("已恢复旧运行版本，聊天和历史数据已保留。")
    except (InstallError, OSError, SyntaxError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
