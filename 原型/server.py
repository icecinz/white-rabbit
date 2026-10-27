#!/usr/bin/env python3
"""One AI: local-only shared chat and read-only native history, stdlib HTTP."""
import argparse
import base64
import errno
import fcntl
import hashlib
import importlib.util
import json
import inspect
import os
import re
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from chat_store import (ChatStore, BusyError, ContextSelectionConflict, SourceUpdateConflict,
                        ProfileSelectionConflict, imported_message, source_update_comparison, source_image_backfill_plan)
from rabbit_preparation import build_context, ContextCapacityError, prepare_text_file_messages
from library import LibraryView, conversation_query, conversation_directory
from preferences import Preferences, management, validate_model_id
import app_settings
import app_management
from rabbit_memory import memory_management
from rabbit_memory import memory_connection_check
import native_model_management
import native_work_status
import external_work
import native_chat_management
from model_catalog import ModelCatalog
from rabbit_content.materials import ImageStore, MAX_FILE_BYTES
from native_history_materials import NativeHistoryMaterials
from profiles import (AIProfiles, ProfileConflict, ProfileCreationConflict, DefaultProfileConflict,
                      PROFILE_FIELDS, UNSET as PROFILE_UNSET, make_profile_snapshot, validate_request_snapshot)
from rabbit_memory.memory_profiles import (MemoryProfiles, MemoryProfileConflict, MemoryProfileCreationConflict, MUTABLE_FIELDS,
                             make_memory_snapshot, validate_memory_profile_revision)
from rabbit_memory import MemoryWriteJobs
from rabbit_memory.hindsight_memory import HindsightMemory, MemoryServiceError
from memory_context import (memory_query, validate_memory_inputs, public_recall_info,
                            memory_context_block, validate_recall_package, selected_memory_block)
from rabbit_memory.native_memory_bindings import NativeMemoryBindings, validate_native_source
from rabbit_memory.native_hindsight_memory import (read_native_source as read_memory_source, probe_native_source,
                                     NativeHindsightReadOnly)
import codex_adapter
from document_tickets import DocumentTickets, DocumentTicketExpired
from rabbit_content.project_publish_store import ProjectPublishStore, ProjectPublishStoreError
from rabbit_content.markdown_chat_registry import MarkdownChatRegistry, source_identity as markdown_source_identity
from rabbit_content.history_markdown_reference import HistoryMarkdownReference
from rabbit_content.markdown_chat_document import render as render_markdown_chat, render_native as render_native_markdown, native_revision
from native_markdown_source import NativeMarkdownSource
from rabbit_content.markdown_chat_sync import MarkdownChatSyncError
from rabbit_content.markdown_sync_files import MarkdownMoveError
import native_task_input
import native_task_result
from rabbit_content.file_intent import validate_intent, validate_inputs
from rabbit_content.file_task_store import FileTaskStore, FileTaskStoreError
from agent_registry import IDS as AGENT_IDS, LABELS as AGENT_LABELS, has as registered_agent, browser_definitions
from history_sources import HistorySources, HistorySourceError
from rabbit_preparation import prepare_file_context
from specialist_http import SpecialistHTTP, SpecialistHTTPError, decode_body as decode_specialist_body
from rabbit_preparation import handoff as native_handoff, MaterialReferenceError
from native_materials import NativeMaterials, NativeMaterialError
from native_profile_catalog import (NativeProfileUnavailable, resolve_native_profile,
                                    project_native_capabilities, project_claude_capabilities, resolve_claude_profile)
from native_chat_selection import (NativeChatSelections, NativeChatSelectionConflict,
                                   NativeChatSelectionCorrupt)
from native_memory_selection import (NativeMemorySelections, NativeMemorySelectionConflict,
                                     NativeMemorySelectionCorrupt)
from rabbit_runtime.pi.profile_catalog import (NATIVE_PREFIX as PI_NATIVE_PREFIX, PiProfileUnavailable,
                                project_pi_capabilities, resolve_pi_profile)
from rabbit_runtime.hermes.profile_catalog import (NATIVE_PREFIX as HERMES_NATIVE_PREFIX, HermesProfileUnavailable,
                                    project_hermes_capabilities, resolve_hermes_profile)
from admission_gate import AdmissionGate, AdmissionClosed
from service_admission import admitted_service, admitted_http, acquire_service_lock
from rabbit_ui import read_resource, ResourceNotFound


AI_PROFILE_UNAVAILABLE = '所保存的连接、模型或强度当前不可用；参数保留，请在管理里核对。'
ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('one_ai_history_indexer', ROOT.parent / '工具' / 'local_history_mcp.py')
INDEXER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INDEXER)
MAX_BODY = 256 * 1024
# Upload base64 is transient transport only. The special endpoint retains its
# own bound; ordinary chat/draft JSON requests remain limited to 256 KiB.
MAX_MATERIAL_BODY = ((MAX_FILE_BYTES + 2) // 3) * 4 + 8192
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_TEXT_MATERIAL_BODY = ((MAX_DOCUMENT_BYTES + 2) // 3) * 4 + 16384
MAX_DOCUMENT_MANIFEST_BYTES = 1024 * 1024
MAX_TEXT_FILE_PREPARATION_BYTES = 8 * 1024 * 1024
MAX_TEXT_FILE_PREPARATION_FILES = 128
DEFAULT_MEMORY_DRIVER = object()
DEFAULT_NATIVE_SOURCE = object()


def hindsight_config_path():
    """The host owns its Hermes integration path; reusable memory code does not."""
    return Path.home() / '.hermes' / 'hindsight' / 'config.json'


def read_native_source(path=None):
    """Re-read the selected file each time so source-change guards stay live."""
    return read_memory_source(hindsight_config_path() if path is None else path)


FILE_WARNINGS = {
    'artifact_incomplete': '有文件尚未完成保存核对，没有列为可用成果。',
    'artifact_changed': '有成果副本已变化或暂不可读，暂不能下载或引用。',
    'input_changed': '任务输入发生变化；已保存的独立成果仍按原版本核验。',
    'file_changed': '执行目录里的文件后来有变化；成果卡保留已核验的保存版本。',
    'scope_changed': '执行目录发生变化，没有跟随换目录。',
    'unsafe_file': '有文件格式或路径不受支持，没有列为成果。',
    'capacity': '有文件超出本轮数量或大小限制，没有列为成果。',
}


class RequestDenied(ValueError):
    pass


class ProfileCreationRejected(ValueError):
    """This create/copy attempt failed before its business writer returned."""
    def __init__(self, error):
        self.status = 404 if isinstance(error, KeyError) else 400
        super().__init__(str(error.args[0]) if isinstance(error, KeyError) else str(error))


class DocumentAccessDenied(ValueError):
    """Only an OS permission denial activates the bounded project mirror."""


class HistoryIndexUnavailable(ValueError):
    pass


class HistorySourceIncomplete(ValueError):
    pass


class MemoryRecallFailed(ValueError):
    """The request was never accepted; callers may safely preserve its draft."""
    def __init__(self):
        super().__init__('这次长期记忆检索没有完成，消息尚未发送，原文字和图片仍保留。请稍后重试，或明确改为不使用长期记忆。')


def documents_root(value):
    """Resolve the explicitly allowed project directory; never infer a home."""
    if value is None:
        return None
    try:
        root = Path(value).expanduser()
        if root.is_symlink() or any(part.startswith('.') for part in root.parts):
            raise ValueError('项目文件目录不能是隐藏目录或符号链接。')
        root = root.resolve(strict=True)
        if any(part.startswith('.') for part in root.parts):
            raise ValueError('项目文件目录不能是隐藏目录或符号链接。')
        if not root.is_dir():
            raise ValueError('项目文件目录不是普通目录。')
        return root
    except (OSError, RuntimeError):
        raise ValueError('项目文件目录不存在或暂时无法读取。') from None


def document_path(root, path):
    """Apply identical scope checks before either live or mirrored reads."""
    if root is None:
        raise ValueError('这个入口尚未启用项目文件预览。')
    if (not isinstance(path, str) or not path or len(path) > 8192
            or any(ord(character) < 32 for character in path)):
        raise ValueError('文件路径无效。')
    try:
        candidate = Path(path).expanduser()
    except (ValueError, RuntimeError):
        raise ValueError('文件路径无效。') from None
    if not candidate.is_absolute():
        candidate = root / candidate
    # abspath normalizes '..' without following a symlink. Actual opens below
    # reject every symlink and only traverse within the configured project.
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError('只能预览这个项目目录内的文件。') from None
    parts = relative.parts
    if not parts or any(part.startswith('.') for part in parts):
        raise ValueError('不能预览隐藏文件或隐藏目录。')
    if candidate.suffix.lower() not in ('.md', '.txt'):
        raise ValueError('目前只支持 Markdown（.md）和文字（.txt）文件。')
    return candidate, relative


def read_local_document(root, path):
    """Read a bounded project file through anchored no-follow handles."""
    candidate, relative = document_path(root, path)
    parts = relative.parts
    descriptors = []
    stage = 'project-directory'
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(root, directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            stage = 'child-directory'
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        stage = 'text-file'
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=current)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('只能预览普通文本文件。')
        if metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError('这份文件超过 512 KiB，请在原应用中打开。')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            stage = 'read-text'
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise ValueError('这份文件超过 512 KiB，请在原应用中打开。')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('这份文件不是 UTF-8 文字，暂时无法预览。') from None
        return {'path': str(candidate), 'name': candidate.name, 'content': content}
    except FileNotFoundError:
        raise ValueError('这份文件不存在，可能已经移动或改名。') from None
    except OSError as error:
        # Private diagnostic contains only a stage and numeric errno, no paths,
        # message text, credentials or file contents.
        print('Project document preview refused: ' + stage + ' errno=' + str(error.errno), file=sys.stderr)
        if error.errno in (errno.EPERM, errno.EACCES):
            raise DocumentAccessDenied('本机后台服务没有读取这份项目文件的权限。') from None
        raise ValueError('文件暂时无法读取；不能预览符号链接或没有读取权限的文件。') from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_document_snapshot(root, path, cache_dir):
    """Read a published mirror entry, never a manifest-supplied local path."""
    candidate, relative = document_path(root, path)
    relative_key = relative.as_posix()
    key = hashlib.sha256(relative_key.encode('utf-8')).hexdigest()
    descriptors = []
    unavailable = '项目文字副本尚未就绪，请双击“打开小白兔”刷新后再试。'
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        cache = os.open(cache_dir, flags)
        descriptors.append(cache)
        manifest_fd = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=cache)
        descriptors.append(manifest_fd)
        metadata = os.fstat(manifest_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_MANIFEST_BYTES:
            raise ValueError(unavailable)
        with os.fdopen(manifest_fd, 'rb', closefd=False) as handle:
            raw = handle.read(MAX_DOCUMENT_MANIFEST_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_MANIFEST_BYTES:
            raise ValueError(unavailable)
        manifest = json.loads(raw.decode('utf-8'))
        if not isinstance(manifest, dict) or manifest.get('version') != 1 or manifest.get('source') != str(root):
            raise ValueError(unavailable)
        files, generation, updated_at = (manifest.get('files'), manifest.get('generation'),
                                         manifest.get('updated_at'))
        if (not isinstance(files, dict) or len(files) > 200
                or not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation)
                or not isinstance(updated_at, str) or not 1 <= len(updated_at) <= 100):
            raise ValueError(unavailable)
        if relative_key not in files:
            raise ValueError('项目文字副本中没有这份文件，请双击“打开小白兔”刷新后再试。')
        if files[relative_key] != key:
            raise ValueError(unavailable)
        generations = os.open('generations', flags, dir_fd=cache)
        descriptors.append(generations)
        directory = os.open(generation, flags, dir_fd=generations)
        descriptors.append(directory)
        descriptor = os.open(key + '.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError(unavailable)
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError(unavailable)
        return {'path': str(candidate), 'name': candidate.name, 'content': raw.decode('utf-8'),
                'snapshot': True, 'updated_at': updated_at}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(unavailable) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class Service:
    def __init__(self, data_dir, index_path, adapters=None, documents_dir=None,
                 *, codex_root=None, hermes_db=None, memory_driver=DEFAULT_MEMORY_DRIVER,
                 native_source_getter=DEFAULT_NATIVE_SOURCE, native_transport_factory=None, file_adapters=None,
                 publication_dir=None, claude_root=None, pi_root=None, specialist_factory=None,
                 specialist_connection=None, specialist_work_document=False, native_chat_connection=None,
                 pi_native_connection=None, hermes_native_connection=None, claude_native_connection=None,
                 dsh_native_connection=None, interface_tasks_enabled=False, dsh_root=None, dsh_decoder=None,
                 markdown_dir=None, memory_program_drivers=None, external_work_reader_factory=None):
        if (memory_program_drivers is not None and
                (type(memory_program_drivers) is not dict or
                 set(memory_program_drivers) - {'hermes_files', 'mem0'})):
            raise ValueError('记忆程序需要宿主明确提供的固定驱动。')
        self._memory_program_drivers = dict(memory_program_drivers or {})
        if external_work_reader_factory is not None and not callable(external_work_reader_factory):
            raise ValueError('外部工作读取器需要可信宿主明确提供。')
        self._external_work = None
        if specialist_factory is not None and specialist_connection is not None:
            raise ValueError('专员只能使用一个可信原生构造入口。')
        if specialist_factory is not None and not callable(specialist_factory):
            raise ValueError('专员构造入口无效。')
        if specialist_connection is not None and not callable(getattr(specialist_connection, 'create', None)):
            raise ValueError('专员连接配置无效。')
        if native_chat_connection is not None and not callable(getattr(native_chat_connection, 'create', None)):
            raise ValueError('原生对话连接配置无效。')
        if pi_native_connection is not None and not callable(getattr(pi_native_connection, 'create_rpc', None)):
            raise ValueError('Pi原生连接配置无效。')
        if hermes_native_connection is not None and not callable(getattr(hermes_native_connection, 'create_rpc', None)):
            raise ValueError('Hermes原生连接配置无效。')
        if claude_native_connection is not None and not callable(getattr(claude_native_connection, 'create_rpc', None)):
            raise ValueError('Claude原生连接配置无效。')
        if dsh_native_connection is not None and not callable(getattr(dsh_native_connection, 'create_rpc', None)):
            raise ValueError('DSH原生连接配置无效。')
        # History reads use a separate pure decoder, never create_rpc(). Custom
        # fixtures do not acquire HOME sources merely by supplying a connector.
        if dsh_decoder is None and (dsh_root is not None or adapters is None):
            from rabbit_runtime.dsh.connection import DshNativeConnection
            if isinstance(dsh_native_connection, DshNativeConnection):
                from dsh_history import DshHistoryDecoder
                dsh_decoder = DshHistoryDecoder(dsh_native_connection.node, dsh_native_connection.package_root)
                if dsh_root is None:
                    home = dsh_native_connection.home or Path(os.environ.get('DSH_HOME') or Path.home()/'.dsh')
                    if not Path(home).is_absolute():
                        raise ValueError('DSH资料目录不是绝对路径，请明确配置历史存储根目录。')
                    dsh_root = Path(home)/'sessions'
        if (dsh_root is None) != (dsh_decoder is None) or dsh_decoder is not None and not callable(dsh_decoder):
            raise ValueError('DSH历史需要明确的存储根目录与只读解码器。')
        if (type(specialist_work_document) is not bool
                or specialist_work_document and specialist_factory is None and specialist_connection is None):
            raise ValueError('专员工作说明只能由已配置的可信原生入口启用。')
        if (type(interface_tasks_enabled) is not bool
                or interface_tasks_enabled and specialist_factory is None and specialist_connection is None):
            raise ValueError('应用交办记录只能由已配置的可信专员入口启用。')
        self._lock_fd = None
        self.instance_id = secrets.token_hex(8)
        self.admission = AdmissionGate(self.instance_id)
        self._cleanup_blockers = set()
        self.file_tasks = None
        self.publications = None
        self.markdown_chats = None
        self.markdown_notice = ''
        self.markdown_root = None
        self.specialist = None
        self._specialist_store = None
        self._specialist_http = None
        self._specialist_work_document = None
        self._specialist_lock = threading.RLock()
        self.interface_tasks = None
        self.native_chats = None
        self.codex_history = None
        self.hermes_history = None
        self.pi_history = None
        self.claude_history = None
        self.dsh_history = None
        self._native_materials = None
        self.pi_native_chats = None
        self.hermes_native_chats = None
        self.claude_native_chats = None
        self.dsh_native_chats = None
        self._memory_thread = None
        self._source_repair_thread = None
        self._memory_stop = threading.Event()
        self._memory_wake = threading.Event()
        self.history_notice = ''
        self.documents_notice = ''
        self.publication_notice = ''
        try:
            self.documents_dir = documents_root(documents_dir)
        except ValueError:
            self.documents_dir = None
            self.documents_notice = '原项目目录暂不可访问，已有聊天与成果保留；恢复目录后重新启动入口再核对。'
        # A preview directory does not silently grant publication rights.
        try:
            self.publication_root = documents_root(publication_dir)
        except ValueError:
            self.publication_root = None
            self.publication_notice = '原项目目录暂不可访问，已有聊天与成果保留；恢复目录后重新启动入口再核对。'
        # Only the trusted host selects the organization root. Existing project
        # publication authority includes its dedicated 对话 subdirectory; the
        # browser cannot supply another root or make startup create directories.
        selected_markdown = markdown_dir if markdown_dir is not None else (
            self.publication_root / '对话' if self.publication_root is not None else None)
        if selected_markdown is not None:
            try:
                self.markdown_root = documents_root(selected_markdown)
            except ValueError:
                self.markdown_notice = '对话文件夹暂不可访问；创建或恢复原项目的“对话”文件夹后，重新启动入口即可核对。'
        self.data_dir = INDEXER.assert_local_index(data_dir)
        self.index_path = INDEXER.assert_local_index(index_path)
        if adapters is None:
            # A real service publishes only its own derived index. Preserve the
            # caller's lexical path until this check, rather than resolving an
            # alias and silently granting it write ownership.
            lexical_data = Path(os.path.abspath(Path(data_dir).expanduser()))
            lexical_index = Path(os.path.abspath(Path(index_path).expanduser()))
            roots = (Path(codex_root or Path.home()/'.codex').expanduser().resolve(),
                     Path(claude_root or Path.home()/'.claude').expanduser().resolve(),
                     (Path.home()/'.hermes').resolve())
            sources = ((Path.home()/'.imcodes/user-history/history.sqlite').resolve(),
                       Path(hermes_db or Path.home()/'.hermes/state.db').expanduser().resolve())
            if (lexical_data != self.data_dir or lexical_index != self.index_path
                    or self.index_path != self.data_dir/'history.sqlite'
                    or self.index_path in sources
                    or any(self.index_path == root or root in self.index_path.parents for root in roots)):
                raise ValueError('真实入口只能使用数据目录内独立的history.sqlite，不能写原生来源、旧插件索引或符号链接。')
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.documents_cache_dir = self.data_dir / 'project-documents'
        self.document_tickets = DocumentTickets()
        try:
            self._lock_fd = acquire_service_lock(self.data_dir)
        except OSError:
            raise BusyError('这个聊天库已有服务在运行，请打开现有入口。')
        try:
            repair_sources = self._initialize_owned(adapters, codex_root, hermes_db, memory_driver,
                native_source_getter, native_transport_factory, file_adapters, claude_root, pi_root,
                dsh_root, dsh_decoder)
            self._external_work = external_work.ExternalWork(self.workspace_id,
                reader_factory=external_work_reader_factory)
            # Only explicit selection writes create selection metadata. Startup
            # and reads neither change the schema nor discover/open an Agent.
            self.native_selections = NativeChatSelections(self.store.path, workspace_id=self.workspace_id)
            self.native_memory_selections = NativeMemorySelections(self.store.path, workspace_id=self.workspace_id)
            if specialist_connection is not None:
                # Ownership is known only after the local ChatStore exists.
                # Connection.create constructs the same lazy holder as a test
                # factory; it cannot open native RPC before HTTP is listening.
                def specialist_factory(store, *, cwd):
                    return specialist_connection.create(store, cwd=cwd, workspace_id=self.workspace_id)
            if specialist_factory is not None:
                self._initialize_specialist(specialist_factory, work_document=specialist_work_document)
            # The flag permits creation only. A former ledger remains visible
            # after disabling execution; maintenance must not forget its work.
            task_path = self.data_dir / 'interface-tasks.sqlite'
            task_exists = os.path.lexists(task_path)
            if interface_tasks_enabled or task_exists:
                from rabbit_specialist.interface_tasks import InterfaceTasks
                self.interface_tasks = InterfaceTasks(task_path, workspace_id=self.workspace_id,
                    create_if_missing=interface_tasks_enabled and not task_exists)
            if any(connection is not None for connection in
                   (native_chat_connection, pi_native_connection, hermes_native_connection, claude_native_connection,
                    dsh_native_connection)):
                self._native_materials = NativeMaterials(self.materials, self.file_tasks,
                                                        self.data_dir / 'native-materials')
            if native_chat_connection is not None:
                from native_chats import NativeChats
                def native_factory(store, *, cwd, session_key):
                    return native_chat_connection.create(store, cwd=cwd, session_key=session_key,
                                                         workspace_id=self.workspace_id)
                self.native_chats = NativeChats(self.data_dir, self.store.path, native_factory,
                                               workspace_id=self.workspace_id, admission=self.admission,
                                               materials=self._native_materials,
                                               memory_recall=self._recall_memory_profile,
                                               memory_query_filter=INDEXER.redact)
                from codex_history_service import CodexHistoryService
                self.codex_history = CodexHistoryService(self, native_chat_connection)
                self.native_chats.adopted_open_checker = self.codex_history.verify_open
            if pi_native_connection is not None:
                from pi_native import PiNativeChats
                self.pi_native_chats = PiNativeChats(self.data_dir, self.store.path, pi_native_connection,
                                                    workspace_id=self.workspace_id, admission=self.admission,
                                                    materials=self._native_materials,
                                                    memory_recall=self._recall_memory_profile,
                                                    memory_query_filter=INDEXER.redact)
                if (isinstance(getattr(pi_native_connection, 'cli_script', None), Path)
                        and self.history_sources.pi_root is not None):
                    from pi_history_adoption import PiHistoryAdoption
                    from native_history_service import NativeHistoryService
                    manager = self.pi_native_chats
                    adoption = PiHistoryAdoption(self.history, self.history_sources.pi_root,
                        manager.catalogue, manager.store, workspace_id=self.workspace_id,
                        excluded_sources=self.native_chat_sources)
                    self.pi_history = NativeHistoryService(self, manager, adoption)
                    manager.adopted_open_checker = self.pi_history.verify_open
            if hermes_native_connection is not None:
                from hermes_native import HermesNativeChats
                self.hermes_native_chats = HermesNativeChats(self.data_dir, self.store.path, hermes_native_connection,
                                                            workspace_id=self.workspace_id, admission=self.admission,
                                                            materials=self._native_materials,
                                                            memory_recall=self._recall_memory_profile,
                                                            memory_query_filter=INDEXER.redact)
                # Only a trusted configured native source can provide the
                # version-bound metadata guard; mock/legacy connectors keep
                # their ordinary renderer without claiming history adoption.
                if (isinstance(getattr(hermes_native_connection, 'source_root', None), Path)
                        and isinstance(getattr(hermes_native_connection, 'native_commit', None), str)):
                    from hermes_history_adoption import HermesHistoryAdoption
                    from hermes_history_guard import HermesHistoryGuard
                    from native_history_service import NativeHistoryService
                    guard = HermesHistoryGuard(hermes_native_connection.source_root,
                                               hermes_native_connection.native_commit)
                    manager = self.hermes_native_chats
                    adoption = HermesHistoryAdoption(self.history, self.native_history_materials.hermes_db,
                        manager.catalogue, manager.store, workspace_id=self.workspace_id,
                        excluded_sources=self.native_chat_sources, guard_resume=guard)
                    self.hermes_history = NativeHistoryService(self, manager, adoption)
                    manager.adopted_open_checker = self.hermes_history.verify_open
            if claude_native_connection is not None:
                from claude_native import ClaudeNativeChats
                self.claude_native_chats = ClaudeNativeChats(self.data_dir, self.store.path, claude_native_connection,
                                                            workspace_id=self.workspace_id, admission=self.admission,
                                                            materials=self._native_materials,
                                                            memory_recall=self._recall_memory_profile,
                                                            memory_query_filter=INDEXER.redact)
                if (isinstance(getattr(claude_native_connection, 'python', None), Path)
                        and isinstance(getattr(claude_native_connection, 'cli', None), Path)
                        and callable(getattr(claude_native_connection, 'resolve_history_project', None))
                        and self.history_sources.claude_root is not None):
                    from claude_history_adoption import ClaudeHistoryAdoption
                    from native_history_service import NativeHistoryService
                    manager = self.claude_native_chats
                    source_root = self.history_sources.claude_root
                    adoption = ClaudeHistoryAdoption(self.history, source_root,
                        manager.catalogue, manager.store, workspace_id=self.workspace_id,
                        excluded_sources=self.native_chat_sources,
                        project_resolver=lambda cwd: claude_native_connection.resolve_history_project(
                            cwd, claude_root=source_root))
                    self.claude_history = NativeHistoryService(self, manager, adoption)
                    manager.adopted_open_checker = self.claude_history.verify_open
            if dsh_native_connection is not None:
                from dsh_native import DshNativeChats
                self.dsh_native_chats = DshNativeChats(self.data_dir, self.store.path, dsh_native_connection,
                                                      workspace_id=self.workspace_id, admission=self.admission,
                                                      materials=self._native_materials,
                                                      memory_recall=self._recall_memory_profile,
                                                      memory_query_filter=INDEXER.redact)
                reader = getattr(dsh_decoder, 'inspect_resume', None)
                if self.history_sources.dsh_root is not None and callable(reader):
                    from dsh_history_adoption import DshHistoryAdoption
                    from native_history_service import NativeHistoryService
                    manager = self.dsh_native_chats
                    adoption = DshHistoryAdoption(self.history, self.history_sources.dsh_root,
                        manager.catalogue, manager.store, workspace_id=self.workspace_id,
                        excluded_sources=self.native_chat_sources, reader=reader)
                    self.dsh_history = NativeHistoryService(self, manager, adoption)
                    manager.adopted_open_checker = self.dsh_history.verify_open
            # All local initialization and recovery checks finish before any
            # background work begins. Memory starts last, with no later setup.
            if repair_sources:
                self._wake_source_repair()
            if self._memory_thread is not None:
                self._memory_thread.start()
        except BaseException:
            try:
                self._abort_initialization()
            except BusyError:
                raise  # A native owner still exists; keep its store and flock.
            except Exception:
                pass  # Cleanup must not replace the original startup failure.
            raise

    def _initialize_specialist(self, factory, *, work_document=False):
        # A trusted in-process factory only constructs a lazy holder; it must
        # not call open/initialize. The Service flock is already held here.
        if not callable(factory):
            raise ValueError('专员构造入口无效。')
        from rabbit_runtime.native_session_store import NativeSessionStore
        self._specialist_store = NativeSessionStore(self.data_dir / 'specialist-native.sqlite')
        cwd = self.data_dir / 'specialist-workspace'
        if cwd.is_symlink():
            raise ValueError('专员工作目录不可用。')
        cwd.mkdir(mode=0o700, exist_ok=True)
        if work_document:
            from rabbit_specialist.specialist_workspace import prepare_workspace
            binding = self._specialist_store.read_binding('specialist/codex')
            self._specialist_work_document = prepare_workspace(cwd,
                self._read_specialist_work_document,
                bound_thread_id=binding.get('thread_id') if binding else None)
        self.specialist = factory(self._specialist_store, cwd=cwd)
        if self.specialist is None:
            raise ValueError('专员构造入口未返回会话持有者。')
        self._specialist_http = SpecialistHTTP(self.specialist, admission=self.admission)
        from specialist_memory import SpecialistMemoryHTTP, capable as shared_memory_specialist
        if shared_memory_specialist(self.specialist):
            self._specialist_http = SpecialistMemoryHTTP(self._specialist_http, self.store.path,
                workspace_id=self.workspace_id, recaller=self._recall_memory_profile, query_filter=INDEXER.redact)

    def _read_specialist_work_document(self):
        # A fixed project document, selected only by the trusted constructor.
        # Reuse the existing no-follow reader and its narrow permission mirror.
        path = '小白兔专员/工作说明.md'
        try:
            value = read_local_document(self.documents_dir, path)
        except DocumentAccessDenied:
            value = read_document_snapshot(self.documents_dir, path, self.documents_cache_dir)
        else:
            value = dict(value, snapshot=False, updated_at=None)
        return dict(value, source_root=str(self.documents_dir))

    def _specialist_library_source(self):
        # Read the authoritative metadata through its existing owner. Do not
        # take the RPC action lock or call holder.view/open from library reads.
        store = self._specialist_store
        if store is None:
            return None
        binding = store.read_binding('specialist/codex')
        thread_id = binding.get('thread_id') if binding is not None else None
        return ('codex', thread_id) if thread_id is not None else None

    @admitted_service
    def specialist_request(self, method, path, *, query='', body=None):
        with self._specialist_lock:
            if self._specialist_http is None:
                raise SpecialistHTTPError(404, 'specialist_disabled', '这台入口尚未启用小白兔专员。',
                    accepted=False if method != 'GET' else None)
            if self._memory_stop.is_set():
                raise SpecialistHTTPError(503, 'specialist_service_closing', '本机服务正在收尾，请稍后核对原回执。',
                    accepted=False if method != 'GET' else None)
            return self._specialist_http.dispatch(method, path, query=query, body=body)

    @admitted_service
    def native_chat_sources(self):
        # Binding metadata outlives execution configuration. Read it directly
        # even when an Agent is disabled, without catalogue/holder construction,
        # owner locks, cwd checks, transcript reads or native RPCs.
        from rabbit_runtime.native_session_store import NativeSessionStore
        from rabbit_runtime.pi.session_store import PiNativeStore
        from rabbit_runtime.hermes.session_store import HermesNativeStore
        from rabbit_runtime.claude.session_store import ClaudeNativeStore
        from rabbit_runtime.dsh.session_store import DSHNativeStore
        result = []
        for name, store_type, manager in (
                ('native-chats.sqlite', NativeSessionStore, self.native_chats),
                ('pi-native.sqlite', PiNativeStore, self.pi_native_chats),
                ('hermes-native.sqlite', HermesNativeStore, self.hermes_native_chats),
                ('claude-native.sqlite', ClaudeNativeStore, self.claude_native_chats),
                ('dsh-native.sqlite', DSHNativeStore, self.dsh_native_chats)):
            path = self.data_dir / name
            if manager is None and not os.path.lexists(path):
                continue
            guard = None
            try:
                if manager is not None:
                    # Reuse the existing metadata owner, never manager.sources
                    # or its action lock. Library invokes us outside ChatStore.
                    result.extend(manager.store.sources())
                    continue
                if store_type is NativeSessionStore:
                    # Its reader constructor takes a blocking shared flock.
                    # Hold a nonblocking shared guard through construction so
                    # an unrelated writer cannot stall disabled-entry listing.
                    guard = os.open(self.data_dir / ('.' + name + '.lock'),
                                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    info = os.fstat(guard)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('Invalid native metadata lock.')
                    fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with store_type(path, readonly=True) as store:
                    result.extend(store.sources())
            except (OSError, ValueError, sqlite3.Error):
                raise BusyError('原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
            finally:
                if guard is not None:
                    os.close(guard)
        return sorted(set(result))

    def enabled_native_agents(self):
        """Configured entry points only; discovery does not inspect owners."""
        return tuple(agent for agent, manager in (
            ('codex', self.native_chats), ('pi', self.pi_native_chats),
            ('hermes', self.hermes_native_chats), ('claude', self.claude_native_chats),
            ('dsh', self.dsh_native_chats))
            if manager is not None and not manager._closing and not getattr(manager, '_closed', False))

    @admitted_service
    def native_chat_bindings(self, *, include_activity=False):
        from rabbit_runtime.native_session_store import NativeSessionStore
        from codex_history_adoption import is_adopted_binding
        path = self.data_dir / 'native-chats.sqlite'
        guard = None
        result = []
        try:
            if self.native_chats is not None:
                rows = self.native_chats.store.source_bindings(include_activity=include_activity)
            elif os.path.lexists(path):
                guard = os.open(self.data_dir / '.native-chats.sqlite.lock',
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(guard)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Invalid native metadata lock.')
                fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with NativeSessionStore(path, readonly=True) as store:
                    rows = store.source_bindings(include_activity=include_activity)
            else:
                rows = []
            result.extend(dict(row, agent='codex', adopted=is_adopted_binding(
                self.workspace_id, row['chat_id'], row['session_id'])) for row in rows)
        except (OSError, ValueError, sqlite3.Error):
            raise BusyError('原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
        finally:
            if guard is not None:
                os.close(guard)
        from rabbit_runtime.hermes.session_store import HermesNativeStore
        from hermes_history_adoption import is_adopted_binding as hermes_adopted
        path = self.data_dir / 'hermes-native.sqlite'
        try:
            if self.hermes_native_chats is not None:
                rows = self.hermes_native_chats.store.source_bindings(include_activity=include_activity)
            elif os.path.lexists(path):
                with HermesNativeStore(path, readonly=True) as store:
                    rows = store.source_bindings(include_activity=include_activity)
            else:
                rows = []
            adopted = {row['chat_id'] for row in rows if hermes_adopted(
                self.workspace_id, row['chat_id'], row['session_id'])}
            result.extend(dict(row, agent='hermes', adopted=row['chat_id'] in adopted) for row in rows)
        except (OSError, ValueError, sqlite3.Error):
            raise BusyError('Hermes原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
        from rabbit_runtime.pi.session_store import PiNativeStore
        from pi_history_adoption import is_adopted_binding as pi_adopted
        path = self.data_dir / 'pi-native.sqlite'
        try:
            if self.pi_native_chats is not None:
                rows = self.pi_native_chats.store.source_bindings(include_activity=include_activity)
            elif os.path.lexists(path):
                with PiNativeStore(path, readonly=True) as store:
                    rows = store.source_bindings(include_activity=include_activity)
            else:
                rows = []
            result.extend(dict(row, agent='pi', adopted=pi_adopted(
                self.workspace_id, row['chat_id'], row['session_id'])) for row in rows)
        except (OSError, ValueError, sqlite3.Error):
            raise BusyError('Pi原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
        from rabbit_runtime.claude.session_store import ClaudeNativeStore
        from claude_history_adoption import is_adopted_binding as claude_adopted
        path = self.data_dir / 'claude-native.sqlite'
        try:
            if self.claude_native_chats is not None:
                rows = self.claude_native_chats.store.source_bindings(include_activity=include_activity)
            elif os.path.lexists(path):
                with ClaudeNativeStore(path, readonly=True) as store:
                    rows = store.source_bindings(include_activity=include_activity)
            else:
                rows = []
            result.extend(dict(row, agent='claude', adopted=claude_adopted(
                self.workspace_id, row['chat_id'], row['session_id'])) for row in rows)
        except (OSError, ValueError, sqlite3.Error):
            raise BusyError('Claude原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
        from rabbit_runtime.dsh.session_store import DSHNativeStore
        path = self.data_dir / 'dsh-native.sqlite'
        try:
            if self.dsh_native_chats is not None:
                rows = self.dsh_native_chats.store.source_bindings(include_activity=include_activity)
            elif os.path.lexists(path):
                with DSHNativeStore(path, readonly=True) as store:
                    rows = store.source_bindings(include_activity=include_activity)
            else:
                rows = []
            from dsh_history_adoption import is_adopted_binding as dsh_adopted
            result.extend(dict(row,agent='dsh',adopted=dsh_adopted(
                self.workspace_id,row['chat_id'],row['session_id'])) for row in rows)
        except (OSError, ValueError, sqlite3.Error):
            raise BusyError('DSH原生对话绑定暂不可读取，请恢复原记录后刷新。') from None
        return result

    @admitted_service
    def codex_history_request(self, action, fields):
        return self.native_history_request('codex', action, fields)

    @admitted_service
    def native_history_request(self, agent, action, fields):
        from native_history_service import _failure
        controllers = {'codex': self.codex_history, 'hermes': self.hermes_history,
                       'pi': self.pi_history, 'claude': self.claude_history, 'dsh': self.dsh_history}
        if type(agent) is not str or agent not in controllers:
            raise _failure('native_history_invalid')
        controller = controllers[agent]
        if controller is None:
            raise _failure('native_history_disabled')
        return controller.dispatch(action, fields)

    @admitted_service
    def native_chat_request(self, method, path, *, query='', body=None):
        if self.native_chats is None:
            raise SpecialistHTTPError(404, 'native_chats_disabled', '这台入口尚未启用原生对话。',
                                      accepted=False if method != 'GET' else None)
        if self._memory_stop.is_set():
            raise SpecialistHTTPError(503, 'native_chats_closing', '本机服务正在收尾，请稍后核对原回执。',
                                      accepted=False if method != 'GET' else None)
        return self._native_configuration_result('codex', method, path,
            self.native_chats.dispatch(method, path, query=query, body=body))

    def _close_native_chats(self):
        if self.codex_history is not None:
            try:
                self.codex_history.close()
            except BaseException:
                raise BusyError('原会话核对进程仍在收尾，记录库与服务锁暂时保留。') from None
            self.codex_history = None
        if self.native_chats is not None:
            try:
                self.native_chats.close()
            except BaseException:
                raise BusyError('原生对话执行器仍在收尾，记录库与服务独占锁暂时保留。') from None
            self.native_chats = None

    @admitted_service
    def pi_native_request(self, method, path, *, query='', body=None):
        if self.pi_native_chats is None:
            raise SpecialistHTTPError(404, 'pi_native_disabled', '这台入口尚未启用Pi原生对话。',
                                      accepted=False if method != 'GET' else None)
        if self._memory_stop.is_set():
            raise SpecialistHTTPError(503, 'pi_native_closing', 'Pi原生入口正在收尾。',
                                      accepted=False if method != 'GET' else None)
        from rabbit_runtime.pi.session_store import PiStoreError
        try:
            return self._native_configuration_result('pi', method, path,
                self.pi_native_chats.dispatch(method, path, query=query, body=body))
        except PiStoreError as error:
            raise SpecialistHTTPError(409, 'pi_native_' + error.code,
                'Pi原生绑定或回执需要核对，未自动覆盖或重发。') from None

    def _close_pi_native_chats(self):
        if self.pi_history is not None:
            try:
                self.pi_history.close()
            except Exception:
                raise BusyError('Pi原会话核验仍在收尾，记录库与服务锁暂时保留。') from None
            self.pi_history = None
        if self.pi_native_chats is not None:
            try:
                self.pi_native_chats.close()
            except BaseException:
                raise BusyError('Pi原生执行器仍在收尾，记录库与服务锁暂时保留。') from None
            self.pi_native_chats = None

    @admitted_service
    def hermes_native_request(self, method, path, *, query='', body=None):
        if self.hermes_native_chats is None:
            raise SpecialistHTTPError(404, 'hermes_native_disabled', '这台入口尚未启用Hermes原生对话。',
                                      accepted=False if method != 'GET' else None)
        if self._memory_stop.is_set():
            raise SpecialistHTTPError(503, 'hermes_native_closing', 'Hermes原生入口正在收尾。',
                                      accepted=False if method != 'GET' else None)
        from rabbit_runtime.hermes.session_store import HermesStoreError
        try:
            return self._native_configuration_result('hermes', method, path,
                self.hermes_native_chats.dispatch(method, path, query=query, body=body))
        except HermesStoreError as error:
            raise SpecialistHTTPError(409, 'hermes_native_' + error.code,
                'Hermes原生绑定或回执需要核对，未自动覆盖或重发。') from None

    def _close_hermes_native_chats(self):
        if self.hermes_history is not None:
            try:
                self.hermes_history.close()
            except BaseException:
                raise BusyError('Hermes原会话核对仍在收尾，记录库与服务锁暂时保留。') from None
            self.hermes_history = None
        if self.hermes_native_chats is not None:
            try:
                self.hermes_native_chats.close()
            except BaseException:
                raise BusyError('Hermes原生执行器仍在收尾，记录库与服务锁暂时保留。') from None
            self.hermes_native_chats = None

    @admitted_service
    def claude_native_request(self, method, path, *, query='', body=None):
        if self.claude_native_chats is None:
            raise SpecialistHTTPError(404, 'claude_native_disabled', '这台入口尚未启用Claude原生对话。',
                                      accepted=False if method != 'GET' else None)
        if self._memory_stop.is_set():
            raise SpecialistHTTPError(503, 'claude_native_closing', 'Claude原生入口正在收尾。',
                                      accepted=False if method != 'GET' else None)
        from rabbit_runtime.claude.session_store import ClaudeStoreError
        try:
            return self._native_configuration_result('claude', method, path,
                self.claude_native_chats.dispatch(method, path, query=query, body=body))
        except ClaudeStoreError as error:
            raise SpecialistHTTPError(409, 'claude_native_' + error.code,
                'Claude原生绑定或回执需要核对，未自动覆盖或重发。') from None

    def _close_claude_native_chats(self):
        if self.claude_native_chats is not None:
            try:
                if self.claude_history is not None:
                    self.claude_history.close()
                self.claude_native_chats.close()
            except BaseException:
                raise BusyError('Claude原生执行器仍在收尾，记录库与服务锁暂时保留。') from None
            self.claude_native_chats = None
            self.claude_history = None

    @admitted_service
    def dsh_native_request(self, method, path, *, query='', body=None):
        if self.dsh_native_chats is None:
            raise SpecialistHTTPError(404, 'dsh_native_disabled', '这台入口尚未启用DSH原生对话。',
                                      accepted=False if method != 'GET' else None)
        if self._memory_stop.is_set():
            raise SpecialistHTTPError(503, 'dsh_native_closing', 'DSH原生入口正在收尾。',
                                      accepted=False if method != 'GET' else None)
        from rabbit_runtime.dsh.session_store import DSHStoreError
        try:
            return self._native_configuration_result('dsh', method, path,
                self.dsh_native_chats.dispatch(method, path, query=query, body=body))
        except DSHStoreError as error:
            raise SpecialistHTTPError(409, 'dsh_native_' + error.code,
                'DSH原生绑定或回执需要核对，未自动覆盖或重发。') from None

    def _close_dsh_native_chats(self):
        if self.dsh_native_chats is not None:
            try:
                if self.dsh_history is not None:
                    self.dsh_history.close()
                self.dsh_native_chats.close()
            except BaseException:
                raise BusyError('DSH原生执行器仍在收尾，记录库与服务锁暂时保留。') from None
            self.dsh_native_chats = None
            self.dsh_history = None

    def _close_specialist(self):
        with self._specialist_lock:
            if self.specialist is not None:
                try:
                    self.specialist.close()
                except BaseException:
                    raise BusyError('专员执行器仍在收尾，记录库与服务独占锁暂时保留。') from None
                self.specialist = None
            if self._specialist_store is not None:
                try:
                    self._specialist_store.close()
                except BaseException:
                    raise BusyError('专员记录库仍在收尾，服务独占锁暂时保留。') from None
                self._specialist_store = None
            self._specialist_http = None

    def _close_interface_tasks(self):
        if self.interface_tasks is not None:
            self.interface_tasks.close()
            self.interface_tasks = None

    def _initialize_owned(self, adapters, codex_root, hermes_db, memory_driver,
                          native_source_getter, native_transport_factory, file_adapters, claude_root, pi_root,
                          dsh_root, dsh_decoder):
        """Prepare owned stores and connectors while the service lock is held."""
        from history_view import HistoryView
        self.store = ChatStore(self.data_dir / 'chat.sqlite')
        chat_identity = os.stat(self.store.path, follow_symlinks=False)
        if not stat.S_ISREG(chat_identity.st_mode):
            raise ValueError('聊天记录库不是原有普通文件。')
        self._maintenance_chat_identity = (chat_identity.st_dev, chat_identity.st_ino)
        # A failed child cleanup survives a service restart as structured local
        # metadata. Never infer that a former process group died from run text.
        with self.store.lock, self.store._open() as db:
            db.execute('CREATE TABLE IF NOT EXISTS service_cleanup_blockers '
                       '(run_id TEXT PRIMARY KEY, code TEXT NOT NULL CHECK(code=\'cleanup_unconfirmed\'))')
            if db.execute('SELECT 1 FROM service_cleanup_blockers LIMIT 1').fetchone():
                self._cleanup_blockers.add('previous_unconfirmed_cleanup')
        # Browser recovery follows this durable local store across service
        # restarts without exposing its path or relying on a session nonce.
        self.workspace_id = hashlib.sha256(str(self.store.path).encode('utf-8')).hexdigest()
        self.store.recover_interrupted()
        self.file_tasks = FileTaskStore(self.data_dir / 'file-tasks')
        self.file_tasks.recover()
        self.materials = ImageStore(self.data_dir / 'materials')
        # Supplying test/custom connectors does not implicitly grant access to
        # native HOME histories. An explicit source is shared by both readers.
        if adapters is not None:
            codex_root = codex_root or self.data_dir/'isolated-history/codex'
            hermes_db = hermes_db or self.data_dir/'isolated-history/hermes.db'
        self.native_history_materials = NativeHistoryMaterials(self.materials, INDEXER, codex_root, hermes_db)
        self.history_sources = HistorySources(self.index_path, indexer=INDEXER,
            codex_root=self.native_history_materials.codex_root,
            hermes_db=self.native_history_materials.hermes_db,
            claude_root=claude_root if claude_root is not None else Path.home()/'.claude' if adapters is None else None,
            pi_root=pi_root if pi_root is not None else Path.home()/'.pi/agent' if adapters is None else None,
            dsh_root=dsh_root, dsh_decoder=dsh_decoder)
        if adapters is None and self.index_path == self.data_dir/'history.sqlite' and not self.index_path.exists():
            try:
                self.history_sources.bootstrap_from(Path.home()/'.imcodes/user-history/history.sqlite')
            except (HistorySourceError, OSError, sqlite3.Error):
                self.history_notice = '历史首次整理未完成，已有聊天仍可使用；来源稳定后可点击更新历史。'
        self.history = HistoryView(self.index_path, materializer=self.native_history_materials)
        self.library = LibraryView(self.store, self.history,
            excluded_source_getter=self._specialist_library_source,
            excluded_sources_getter=self.native_chat_sources,
            enabled_native_agents_getter=self.enabled_native_agents,
            native_bindings_getter=lambda: self.native_chat_bindings(include_activity=True))
        self.preferences = Preferences(self.data_dir / 'preferences.json')
        self.workspace = self.data_dir / 'workspace'
        self.workspace.mkdir(mode=0o700, exist_ok=True)
        self.adapters = adapters if adapters is not None else {'codex': codex_adapter}
        if adapters is None:
            try:
                import hermes_adapter
                self.adapters['hermes'] = hermes_adapter
            except ImportError:
                pass
            import claude_adapter
            self.adapters['claude'] = claude_adapter
            import pi_adapter
            import dsh_adapter
            self.adapters['pi'] = pi_adapter
            self.adapters['dsh'] = dsh_adapter
        self.file_adapters = dict(file_adapters or {})
        if adapters is None and file_adapters is None:
            import codex_file_task
            import hermes_file_task
            self.file_adapters = {'codex': codex_file_task, 'hermes': hermes_file_task}
        self.model_catalog = ModelCatalog(self.adapters)
        self.profiles = AIProfiles(self.store.path, capability_validator=self._validate_ai_profile)
        self.memory_profiles = MemoryProfiles(self.store.path)
        self.native_memory_bindings = NativeMemoryBindings(self.store.path)
        self.memory_jobs = MemoryWriteJobs(self.store.path)
        # Invented adapter fixtures never silently contact a real home service.
        self._memory_enabled = adapters is None if memory_driver is DEFAULT_MEMORY_DRIVER else memory_driver is not None
        self._memory_driver_override = None if memory_driver is DEFAULT_MEMORY_DRIVER else memory_driver
        self._native_source_enabled = (adapters is None if native_source_getter is DEFAULT_NATIVE_SOURCE
                                       else callable(native_source_getter))
        self._native_source_getter = read_native_source if native_source_getter is DEFAULT_NATIVE_SOURCE else native_source_getter
        self._native_transport_factory = native_transport_factory
        self._memory_cache = {}
        self._memory_cache_lock = threading.RLock()
        self.memory_runner = None
        self.default_profile_ids = {}
        self.profile_migration_notice = ''
        self._migrate_profile_parameters()
        self.csrf_token = secrets.token_urlsafe(32)
        # Public instance identity changes on restart, independently of the
        # private CSRF nonce and durable counters. Rollback/re-upgrade changes
        # are therefore noticed even when an older program wrote no counters.
        self.workers = {}
        self.worker_lock = threading.RLock()
        self.publication_lock = threading.RLock()
        if self.publication_root is not None:
            try:
                self.publications = ProjectPublishStore(self.data_dir / 'project-publications', self.publication_root)
                self.publications.recover_accepted()
            except (ProjectPublishStoreError, OSError, sqlite3.Error):
                # A revoked project permission must not take existing chat,
                # memory or local artifacts offline on the next launch.
                if self.publications is not None:
                    self.publications.close()
                self.publications = None
                self.publication_notice = '原项目或提交记录暂不可访问，已有聊天与成果保留；恢复权限后重新启动入口再核对。'
        if self.markdown_root is not None:
            try:
                self.markdown_chats = MarkdownChatRegistry(self.data_dir / 'markdown-chats',
                    self.markdown_root, workspace_id=self.workspace_id)
                self.markdown_chats.recover_accepted()
            except (ProjectPublishStoreError, OSError, sqlite3.Error, ValueError):
                if self.markdown_chats is not None:
                    self.markdown_chats.close()
                self.markdown_chats = None
                self.markdown_notice = '对话文件夹或整理记录暂不可访问，已有聊天和文件保留。'
        self.refresh_lock = threading.Lock()
        self.backend_cache = None
        self.backend_checked_at = 0
        if self._memory_enabled:
            from rabbit_memory import MemoryWriteRunner
            self.memory_runner = MemoryWriteRunner(self.memory_jobs, self._memory_driver, self._memory_fingerprint,
                                                  admission=self.admission)
            # The acquired service flock establishes that the former service
            # process is absent. No runner thread exists yet in this instance.
            self.memory_runner.recover(service_lock_held=True)
            self._memory_thread = threading.Thread(target=self._memory_loop, daemon=True)
        return any(not item['available'] for item in self.file_tasks.list_sources(limit=200))

    def _abort_initialization(self):
        """Release constructed resources; never leave an abandoned service lock."""
        self._memory_stop.set()
        self._memory_wake.set()
        self._close_specialist()
        self._close_native_chats()
        self._close_pi_native_chats()
        self._close_hermes_native_chats()
        self._close_claude_native_chats()
        self._close_dsh_native_chats()
        threads = (self._memory_thread, self._source_repair_thread)
        for thread in threads:
            if thread is not None and thread.ident is not None:
                thread.join(timeout=50)
        if any(thread is not None and thread.is_alive() for thread in threads):
            # A transport still finishing owns these resources. Its process
            # must exit before another service may safely recover this store.
            return
        self._close_interface_tasks()
        try:
            if self.markdown_chats is not None:
                self.markdown_chats.close()
            if self.publications is not None:
                self.publications.close()
        finally:
            try:
                if self.file_tasks is not None:
                    self.file_tasks.close()
            finally:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None

    def _wake_source_repair(self):
        # Publication repairs only an already accepted immutable payload. GET
        # receipts stay read-only; no source path or new caller body is read.
        with self.worker_lock:
            if self._memory_stop.is_set():
                return
            if self._source_repair_thread is None or not self._source_repair_thread.is_alive():
                self._source_repair_thread = threading.Thread(target=self._source_repair_loop, daemon=True)
                self._source_repair_thread.start()

    def _source_repair_loop(self):
        delay = 2
        while not self._memory_stop.is_set():
            try:
                if not self._repair_sources_once():
                    return
            except AdmissionClosed:
                pass  # Prepared maintenance must not start another repair.
            if self._memory_stop.wait(delay):
                return
            delay = min(delay * 2, 30)

    def _repair_sources_once(self):
        with self.admission.lease('repair'):
            try:
                pending = self.file_tasks.recover_sources()['pending']
            except (FileTaskStoreError, OSError):
                pending = True
            if not pending:
                # Acceptance and the final recheck share worker_lock. Both
                # recover calls remain under this same repair admission.
                with self.worker_lock:
                    try:
                        pending = self.file_tasks.recover_sources()['pending']
                    except (FileTaskStoreError, OSError):
                        pending = True
                    if not pending:
                        self._source_repair_thread = None
            return bool(pending)

    def maintenance_probe(self):
        from service_maintenance import inspect_service
        return inspect_service(self)

    @admitted_service
    def backends(self, fresh=False):
        if self.backend_cache is None or fresh or time.monotonic() - self.backend_checked_at > 60:
            rows = []
            for backend in AGENT_IDS:
                if backend not in self.adapters:
                    continue
                label = AGENT_LABELS[backend]
                adapter = self.adapters.get(backend)
                try:
                    state = adapter.available() if adapter else {'available': False, 'reason': '接入尚未就绪。'}
                except Exception:
                    state = {'available': False, 'reason': '本机连接检查未完成。'}
                rows.append(dict(state, id=backend, label=label))
            self.backend_cache = rows
            self.backend_checked_at = time.monotonic()
        return self.backend_cache

    @admitted_service
    def status(self):
        try:
            history = self.history.stats()
        except Exception:
            history = {'agents': {}, 'built_at': None,
                       'notice': self.history_notice or '历史索引未就绪，请点击更新历史。'}
        history.setdefault('notice', '已接入用户和助手文字及可读取的原生图片；图片缺失会单独说明，工具现场尚未接入。')
        return {'history': history, 'backends': self.backends(), 'active_runs': self.store.active_count(),
                'native_chats': {'enabled': self.native_chats is not None, 'max_connections': 2},
                'pi_native_chats': {'enabled': self.pi_native_chats is not None, 'max_connections': 1},
                'hermes_native_chats': {'enabled': self.hermes_native_chats is not None, 'max_connections': 1},
                'native_history': {'enabled_agents': [agent for agent, controller in (
                    ('codex', self.codex_history), ('hermes', self.hermes_history), ('pi', self.pi_history),
                    ('claude', self.claude_history), ('dsh', self.dsh_history))
                    if controller is not None and not controller._closed]},
                'claude_native_chats': {'enabled': self.claude_native_chats is not None, 'max_connections': 1},
                'dsh_native_chats': {'enabled': self.dsh_native_chats is not None, 'max_connections': 1},
                'specialist': dict({'enabled': self._specialist_http is not None},
                    **({'work_document': dict(self._specialist_work_document)}
                       if self._specialist_work_document is not None else {})),
                'csrf_token': self.csrf_token, 'workspace_id': self.workspace_id,
                'version': '0.1.0', 'local_only': True,
                'materials': self.materials.capabilities(),
                'files': {'enabled': bool(self.file_adapters), 'backends': list(self.file_adapters),
                          'extensions': ['.md', '.txt'], 'max_input_files': 4,
                          'upload_enabled': True, 'max_material_bytes': MAX_DOCUMENT_BYTES},
                'project_publication': {'enabled': self.publications is not None,
                    'project_name': self.publication_root.name if self.publication_root else None,
                    'project_root': str(self.publication_root) if self.publication_root else None,
                    'notice': self.publication_notice,
                    'max_bytes': MAX_DOCUMENT_BYTES, 'extensions': ['.md', '.txt']},
                'markdown_chats': {'enabled': self.markdown_chats is not None,
                    'root': str(self.markdown_root) if self.markdown_root is not None else None,
                    'max_bytes': 512 * 1024, 'notice': self.markdown_notice,
                    'sync_enabled': self.markdown_chats is not None and self.markdown_chats.sync.supported},
                'profiles': {'enabled': True, 'version': 2, 'memory': 'hindsight' if self._memory_enabled else 'none'},
                'documents': {'enabled': self.documents_dir is not None,
                              'root': str(self.documents_dir) if self.documents_dir is not None else None,
                              'notice': self.documents_notice}}

    @admitted_service
    def management_inventory(self, fresh=False):
        result = management(self.backends(fresh=fresh))
        result['models'] = self.model_catalog.models(fresh=fresh)
        result['native_profile_capabilities'] = self._native_profile_capabilities()
        return result

    @admitted_service
    def work_status(self):
        return native_work_status.snapshot(self)

    @admitted_service
    def external_work_snapshot(self):
        return self._external_work.read()

    @admitted_service
    def external_work_operation(self, body):
        return self._external_work.operation(body)

    def _native_profile_capabilities(self):
        native = self.native_chats
        return {'codex': project_native_capabilities(
            native.catalogue_snapshot() if native is not None else None, enabled=native is not None),
            'pi': project_pi_capabilities(
                self.pi_native_chats.catalogue_snapshot() if self.pi_native_chats is not None else None,
                enabled=self.pi_native_chats is not None),
            'hermes': project_hermes_capabilities(
                self.hermes_native_chats.catalogue_snapshot()
                if self.hermes_native_chats is not None and self.hermes_native_chats.profiles_enabled else None,
                enabled=self.hermes_native_chats is not None and self.hermes_native_chats.profiles_enabled),
            'claude': project_claude_capabilities(
                self.claude_native_chats.catalogue_snapshot()
                if self.claude_native_chats is not None and self.claude_native_chats.profiles_enabled else None,
                enabled=self.claude_native_chats is not None and self.claude_native_chats.profiles_enabled)}

    @admitted_service
    def saved_ai_profile_inventory(self):
        return dict(app_management.saved_ai_profiles(self.profiles), workspace_id=self.workspace_id,
                    native_profile_capabilities=self._native_profile_capabilities())

    @staticmethod
    def _native_configuration_payload(value):
        return dict(value, scope='native_next_turn_selection',
                    inference_performed=False, connection_checked=False)

    def _native_configuration_agent(self, agent):
        controllers = {'codex': self.native_chats, 'pi': self.pi_native_chats,
                       'hermes': self.hermes_native_chats, 'claude': self.claude_native_chats}
        if type(agent) is not str or agent not in controllers:
            raise SpecialistHTTPError(400, 'native_chat_selection_invalid',
                '当前支持普通Codex、Pi、Hermes和Claude对话的下轮配置。', accepted=False)
        if (controllers[agent] is None or
                agent in ('hermes', 'claude') and not controllers[agent].profiles_enabled):
            raise SpecialistHTTPError(503, 'native_chat_selection_unavailable',
                '这台入口尚未启用所选原生Agent。', accepted=False)

    @admitted_service
    def native_chat_configuration(self, agent, chat_id):
        self._native_configuration_agent(agent)
        try:
            value = self.native_selections.read(agent, chat_id)
        except KeyError:
            raise SpecialistHTTPError(404, 'native_chat_selection_not_found',
                '找不到指定的原生对话。', accepted=False) from None
        return self._native_configuration_payload(value)

    @admitted_service
    def select_native_chat_configuration(self, body):
        fields = self._profile_write_fields(body, ('agent', 'chat_id', 'expected_revision',
                                                  'ai_profile_id', 'ai_profile_revision'))
        if set(fields) != {'agent', 'chat_id', 'expected_revision', 'ai_profile_id', 'ai_profile_revision'}:
            raise ValueError('请明确指定对话、原选择版本及成对的配置编号和版本。')
        self._native_configuration_agent(fields['agent'])
        try:
            value = self.native_selections.select(**fields)
        except KeyError:
            raise SpecialistHTTPError(404, 'native_chat_selection_not_found',
                '找不到指定的原生对话。', accepted=False) from None
        return self._native_configuration_payload(value)

    @staticmethod
    def _native_memory_configuration_payload(value):
        return dict(value, scope='native_next_turn_memory_selection',
                    inference_performed=False, connection_checked=False)

    def _native_memory_configuration_agent(self, agent):
        controllers = {'codex': self.native_chats, 'pi': self.pi_native_chats,
                       'hermes': self.hermes_native_chats, 'claude': self.claude_native_chats,
                       'dsh': self.dsh_native_chats}
        if type(agent) is not str or agent not in controllers:
            raise SpecialistHTTPError(400, 'native_chat_memory_selection_invalid',
                '当前支持普通Codex、Pi、Hermes、Claude和DSH对话的下轮共享记忆选择。', accepted=False)
        if (controllers[agent] is None or
                agent == 'pi' and not callable(getattr(controllers[agent].connection, 'model_capabilities', None)) or
                agent == 'hermes' and not controllers[agent].profiles_enabled or
                agent in ('claude', 'dsh') and not callable(getattr(
                    getattr(controllers[agent], 'memory_selections', None), 'none_fence', None))):
            raise SpecialistHTTPError(503, 'native_chat_memory_selection_unavailable',
                '这台入口尚未启用所选原生Agent的记忆选择保护。', accepted=False)

    @admitted_service
    def native_chat_memory_configuration(self, agent, chat_id):
        self._native_memory_configuration_agent(agent)
        try:
            value = self.native_memory_selections.read(agent, chat_id)
        except KeyError:
            raise SpecialistHTTPError(404, 'native_chat_memory_selection_not_found',
                '找不到指定的原生对话。', accepted=False) from None
        return self._native_memory_configuration_payload(value)

    @admitted_service
    def select_native_chat_memory_configuration(self, body):
        fields = self._profile_write_fields(body, ('agent', 'chat_id', 'expected_revision',
                                                  'memory_profile_id', 'memory_profile_revision'))
        if set(fields) != {'agent', 'chat_id', 'expected_revision', 'memory_profile_id', 'memory_profile_revision'}:
            raise ValueError('请明确指定对话、原选择版本及成对的记忆配置编号和版本。')
        self._native_memory_configuration_agent(fields['agent'])
        try:
            value = self.native_memory_selections.select(**fields)
        except KeyError:
            raise SpecialistHTTPError(404, 'native_chat_memory_selection_not_found',
                '找不到指定的原生对话。', accepted=False) from None
        return self._native_memory_configuration_payload(value)

    def _native_configuration_result(self, agent, method, path, value):
        roots = {'codex': '/api/native-chats/', 'pi': '/api/pi-chats/',
                 'hermes': '/api/hermes-chats/', 'claude': '/api/claude-chats/',
                 'dsh': '/api/dsh-chats/'}
        prefix = roots[agent]
        if method == 'GET' and path.startswith(prefix) and '/' not in path[len(prefix):]:
            if agent in ('codex', 'pi', 'hermes', 'claude'):
                try:
                    selection = self.native_selections.read(agent, path[len(prefix):])
                except NativeChatSelectionCorrupt:
                    # Configuration metadata must not hide accepted/unknown native
                    # receipts. The client blocks fresh sends while recovery stays
                    # available through this view and the dedicated receipt route.
                    value = dict(value, configuration_selection_error={
                        'code': 'native_chat_selection_corrupt',
                        'message': '下轮配置记录无法核对；已有消息和回执仍保留。'})
                else:
                    value = dict(value, configuration_selection=selection)
            try:
                memory = self.native_memory_selections.read(agent, path[len(prefix):])
            except NativeMemorySelectionCorrupt:
                value = dict(value, memory_selection_error={
                    'code': 'native_chat_memory_selection_corrupt',
                    'message': '下轮记忆选择无法核对；已有消息和回执仍保留。'})
            else:
                value = dict(value, memory_selection=memory)
        return value

    @admitted_service
    def native_model_connections(self):
        return native_model_management.connections(self)

    @admitted_service
    def refresh_native_model_catalog(self, fields):
        return native_model_management.refresh(self, fields)

    @admitted_service
    def refresh_specialist_model_catalog(self):
        return native_model_management.refresh_specialist(self)

    @admitted_service
    def prepare_native_model_catalog(self, body):
        fields = self._profile_write_fields(body, ('agent', 'chat_id'))
        return native_model_management.prepare(self, fields)

    @admitted_service
    def native_chat_names(self):
        return native_chat_management.names(self)

    @admitted_service
    def conversations_list(self, query='', agent='all', offset=0, limit=20):
        options = conversation_query(query, agent, offset, limit)
        page = self.list_library(**options, include_internal=False)
        return conversation_directory(page, workspace_id=self.workspace_id, options=options)

    def _native_task_payload(self, fields, *, forwarded=False, refresh_native=False):
        result = native_task_result.read(self, fields['agent'], fields['chat_id'],
            fields['client_id'], refresh_native=refresh_native)
        return dict(result, workspace_id=self.workspace_id, scope='native_task/v1',
            agent=fields['agent'], chat_id=fields['chat_id'], client_id=fields['client_id'],
            forwarded=forwarded)

    @admitted_service
    def dispatch_native_task(self, body):
        fields = {key: value for key, value in body.items() if key != '_one_ai_csrf'} if type(body) is dict else body
        submitted = native_task_input.dispatch(self, fields)
        # The POST acknowledgement reads only original submission metadata.
        # Fetching result text is a separate explicit read, never another send.
        try:
            return self._native_task_payload(fields, forwarded=submitted['forwarded'])
        except Exception:
            # Admission already happened. Even a later read's local rejection
            # cannot turn this submission into an unsent acknowledgement.
            raise native_task_input.NativeTaskInputError('submission_unknown', 'unknown') from None

    @admitted_service
    def read_native_task(self, agent, chat_id, client_id):
        return self._native_task_payload(dict(agent=agent, chat_id=chat_id, client_id=client_id),
            refresh_native=True)

    @admitted_service
    def rename_native_chat(self, body):
        try:
            fields = self._profile_write_fields(body, native_chat_management.FIELDS)
        except (ValueError, TypeError):
            native_chat_management.invalid(self)
        return native_chat_management.rename(self, fields)

    @admitted_service
    def rename_ai_profile(self, profile_id, body):
        fields = self._profile_write_fields(body, ('revision', 'name'))
        return dict(app_management.rename_ai_profile(self.profiles, profile_id, fields), workspace_id=self.workspace_id)

    @admitted_service
    def ai_profile_creation_receipt(self, creation_id):
        return dict(app_management.ai_profile_creation(self.profiles, creation_id), workspace_id=self.workspace_id)

    @admitted_service
    def apply_ai_profile_operation(self, body):
        fields = self._profile_write_fields(body,
            ('action', 'creation_id', 'profile', 'profile_id', 'revision', 'changes', 'default_revision'))
        result = app_management.apply_ai_profile_operation(self.profiles, fields)
        if fields.get('action') == 'set_default':
            self._resolve_default_profile_notice()
        return dict(result, workspace_id=self.workspace_id)

    @admitted_service
    def saved_memory_profile_inventory(self):
        return dict(memory_management.saved_memory_profiles(self.memory_profiles,
            self.native_memory_bindings), workspace_id=self.workspace_id)

    @admitted_service
    def memory_profile_creation_receipt(self, creation_id):
        return dict(memory_management.memory_profile_creation(self.memory_profiles,
            self.native_memory_bindings, creation_id), workspace_id=self.workspace_id)

    @admitted_service
    def apply_memory_profile_operation(self, body):
        fields = self._profile_write_fields(body,
            ('action', 'creation_id', 'profile', 'profile_id', 'revision', 'changes'))
        result = memory_management.apply_memory_profile_operation(self.memory_profiles,
            self.native_memory_bindings, fields)
        profile = result['profile']
        if profile['program'] == 'hermes_files' and fields['action'] in ('create', 'copy'):
            # Explicit local creation only; no service/model call and no import
            # of the original Hermes files. A failed setup leaves saved intent.
            try:
                self._memory_driver_for_profile(profile).ensure_bank(profile)
            except (MemoryServiceError, ValueError, OSError):
                pass
        return dict(result, workspace_id=self.workspace_id)

    @admitted_service
    def check_memory_connection(self, profile_id, revision):
        return dict(memory_connection_check.check_memory_connection(self.memory_profiles,
            self.native_memory_bindings, self._memory_driver_for_profile, profile_id, revision),
            workspace_id=self.workspace_id)

    @admitted_service
    def memory_document(self, fields, *, writing=False):
        allowed = ('profile_id', 'name', 'text', 'expected_sha256') if writing else ('profile_id', 'name')
        value = self._profile_write_fields(fields, allowed)
        if set(value) != set(allowed) or value.get('name') not in ('MEMORY.md', 'USER.md'):
            raise ValueError('请选择明确的记忆配置和文档。')
        profile = self.memory_profiles.get(value['profile_id'])
        if profile['program'] != 'hermes_files' or self.native_memory_bindings.get(profile['id']) is not None:
            raise ValueError('此入口只编辑小白兔独立的 Hermes 记忆文档。')
        driver = self._memory_driver_for_profile(profile)
        try:
            document = (driver.write_document(profile, value['name'], value['text'],
                        expected_sha256=value['expected_sha256']) if writing else
                        driver.read_document(profile, value['name']))
        except MemoryServiceError as error:
            raise SpecialistHTTPError(error.status or 503, error.code, str(error),
                **({} if writing else {'accepted': False})) from None
        return dict(scope='memory_document', profile_id=profile['id'], program='hermes_files',
                    document=document, workspace_id=self.workspace_id)

    def _migrate_profile_parameters(self):
        # Preserve parameter intent even if a formerly used connection is now
        # offline. Migration is startup work, never an inference or GET side
        # effect, and it never manufactures past request snapshots.
        for backend in AGENT_IDS:
            if backend not in self.adapters:
                continue
            label = AGENT_LABELS[backend]
            try:
                profile = self.profiles.import_legacy('default:' + backend, label + ' 默认', 'local/' + backend)
            except (ValueError, KeyError):
                self.profile_migration_notice = '部分旧组合未生成配置，原选择保留；请在管理里检查配置。'
                continue
            self.default_profile_ids[backend] = profile['id']
        had_preferences = self.preferences.path.exists()
        try:
            settings = self.preferences.read()
        except ValueError:
            self.profile_migration_notice = '本地设置暂时无法读取，原文件保留；请从已有 AI 配置中选择。'
            return  # A damaged appearance file must not take chat offline.
        combinations = {(backend, model_id) for backend, model_id in settings['default_models'].items()}
        combinations.update((backend, settings['chat_models'].get(chat_id, ''))
                            for chat_id, backend in settings['chat_backends'].items())
        for backend, model_id in sorted(combinations):
            if not model_id:
                continue
            name = (AGENT_LABELS[backend] + ' · ' + model_id.split('/', 2)[-1])[:40]
            try:
                migrated = self.profiles.import_legacy('legacy:' + backend + ':' + model_id, name, 'local/' + backend, model_id=model_id)
            except (ValueError, KeyError):
                self.profile_migration_notice = '部分旧组合未生成配置，原选择保留；请在管理里检查配置。'
                continue
            if settings['default_models'].get(backend) == model_id:
                self.default_profile_ids[backend] = migrated['id']
        try:
            saved_default = self.profiles.get_default()
        except (ValueError, KeyError):
            if not self.profile_migration_notice:
                self.profile_migration_notice = '原默认 AI 配置暂不可读取，原选择保留；请从已有配置中选择。'
            return
        if saved_default is not None:
            return  # An offline saved default is still the user's intent.
        default_id = self.default_profile_ids.get(settings['default_backend'])
        if default_id is None and not had_preferences:
            # A fresh isolated entry has no Codex preference to preserve. Use
            # an injected Agent's own default without guessing its model.
            default_id = next(iter(self.default_profile_ids.values()), None)
        if default_id is not None:
            self.profiles.set_default(default_id)
        elif had_preferences and not self.profile_migration_notice:
            self.profile_migration_notice = '原默认 Agent 当前未接入，原选择保留；请从已有 AI 配置中选择。'

    def _default_ai_profile_id(self):
        try:
            return self.profiles.get_default()
        except (ValueError, KeyError):
            # Keep an unreadable/dangling setting intact. A missing default
            # leaves the existing configurations selectable, not fabricated.
            return None

    def _resolve_ai_profile(self, profile):
        backend = profile['agent_instance_id'].split('/', 1)[1]
        reserved = {'pi': PI_NATIVE_PREFIX, 'hermes': HERMES_NATIVE_PREFIX}.get(backend)
        if reserved and profile['model_id'].startswith(reserved):
            # A native reference is an identity digest, never a text-adapter
            # model name. The generic syntax intentionally accepts slashes.
            raise ValueError('这份模型引用只用于原生对话，不能交给普通文字连接。')
        runner = getattr(self.adapters.get(backend), 'run', None)
        if not self._accepts_keyword(runner, 'route'):
            raise ValueError('这条 Agent 接入器尚不能执行冻结的 AI 配置，原参数仍保留。')
        effort = profile['reasoning_effort']
        try:
            if effort is None:
                route = self.model_catalog.resolve(backend, profile['model_id'])
            else:
                parameters = inspect.signature(self.model_catalog.resolve).parameters
                if 'reasoning_effort' not in parameters:
                    raise ValueError('这条连接尚未接入独立强度控制，不能忽略所选强度。')
                route = self.model_catalog.resolve(backend, profile['model_id'], reasoning_effort=effort)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            # Native connector checks can fail after the directory was cached.
            # Normalize at the boundary before any submission is accepted.
            raise ValueError(AI_PROFILE_UNAVAILABLE) from None
        return dict(route, requested_model_id=profile['model_id'])

    def _validate_ai_profile(self, fields):
        try:
            self._resolve_ai_profile(fields)
        except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError):
            native = {'local/codex': self.native_chats, 'local/pi': self.pi_native_chats,
                      'local/hermes': self.hermes_native_chats,
                      'local/claude': self.claude_native_chats}.get(fields['agent_instance_id'])
            if native is None or fields['device_id'] != 'local':
                raise
            self._resolve_native_profile_from_cache(fields)

    def _resolve_native_profile_from_cache(self, profile):
        """Validate saved parameters against RAM discovery, never open or RPC."""
        backend = profile['agent_instance_id']
        if backend == 'local/claude':
            native = self.claude_native_chats
            if native is None or not native.profiles_enabled:
                raise NativeProfileUnavailable('这台入口尚未启用 Claude 原生模型配置。')
            if profile['device_id'] != 'local':
                raise NativeProfileUnavailable('当前只核对本机 Claude 的原生模型配置。')
            if not profile['model_id'] and profile['reasoning_effort'] is None:
                return {}
            snapshot = native.catalogue_snapshot()
            if snapshot is None:
                raise NativeProfileUnavailable('尚未观察到 Claude SDK 模型候选；请先打开原生对话并更新目录。')
            return resolve_claude_profile(profile, snapshot['catalogue'])
        if backend == 'local/hermes':
            native = self.hermes_native_chats
            if native is None or not native.profiles_enabled:
                raise NativeProfileUnavailable('这台入口尚未启用 Hermes 原生会话配置校验层。')
            if profile['device_id'] != 'local':
                raise NativeProfileUnavailable('当前只核对本机 Hermes 的原生 AI 配置。')
            if not profile['model_id']:
                # Hermes accepts a native reasoning request independently of
                # model selection. The holder freezes actual current identity
                # before sending; management never guesses that live identity.
                try:
                    return resolve_hermes_profile(profile, None)
                except HermesProfileUnavailable as error:
                    raise NativeProfileUnavailable(str(error)) from None
            snapshot = native.catalogue_snapshot()
            if snapshot is None:
                raise NativeProfileUnavailable('尚未观察到 Hermes 原生模型目录；请先打开原生对话并更新配置列表。')
            try:
                return resolve_hermes_profile(profile, snapshot['catalogue'])
            except HermesProfileUnavailable as error:
                raise NativeProfileUnavailable(str(error)) from None
        if backend == 'local/pi':
            native = self.pi_native_chats
            if native is None:
                raise NativeProfileUnavailable('这台入口尚未启用原生 Pi 对话。')
            if profile['device_id'] != 'local':
                raise NativeProfileUnavailable('当前只核对本机 Pi 的原生 AI 配置。')
            if not profile['model_id'] and profile['reasoning_effort'] is None:
                return {}
            snapshot = native.catalogue_snapshot()
            if snapshot is None:
                raise NativeProfileUnavailable('尚未观察到 Pi 原生模型目录；请先打开原生对话并更新配置列表。')
            try:
                return resolve_pi_profile(profile, snapshot['catalogue'])
            except PiProfileUnavailable as error:
                raise NativeProfileUnavailable(str(error)) from None
        native = self.native_chats
        if native is None:
            raise NativeProfileUnavailable('这台入口尚未启用原生 Codex 对话。')
        if profile['agent_instance_id'] != 'local/codex' or profile['device_id'] != 'local':
            raise NativeProfileUnavailable('当前只核对本机 Codex 的原生 AI 配置。')
        if not profile['model_id'] and profile['reasoning_effort'] is None:
            return {}  # Preserve inheritance intent without inventing defaults.
        snapshot = native.catalogue_snapshot()
        if snapshot is None:
            raise NativeProfileUnavailable('尚未观察到原生 Codex 模型目录；请先打开一段原生对话并更新配置列表。')
        return resolve_native_profile(profile, snapshot['catalogue'])

    def _public_ai_profile(self, profile):
        result = dict(profile, available=False, reason='', effective_model=None, effective_effort=None,
                      native_available=False, native_reason=None)
        try:
            route = self._resolve_ai_profile(profile)
            result.update(available=True, effective_model=route.get('model'), effective_effort=route.get('reasoning_effort'))
        except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError):
            result['reason'] = AI_PROFILE_UNAVAILABLE
        try:
            self._resolve_native_profile_from_cache(profile)
        except NativeProfileUnavailable as error:
            result['native_reason'] = str(error)
        else:
            result['native_available'] = True
        return result

    def profiles_revision(self, *, saved=None):
        value = {'ai_state': self.profiles.saved_state() if saved is None else saved,
                 'memory_profiles': self.memory_profiles.list()}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _memory_driver(self):
        if not self._memory_enabled:
            raise MemoryServiceError('memory_service_unavailable', '本机长期记忆连接尚未启用。')
        if self._memory_driver_override is not None:
            return self._memory_driver_override
        try:
            return HindsightMemory.from_config(hindsight_config_path())
        except (OSError, ValueError):
            raise MemoryServiceError('memory_service_unavailable', '本机长期记忆连接配置暂时无法读取。') from None

    def _memory_fingerprint(self, driver=None):
        driver = driver or self._memory_driver()
        # Private identity binds an outbox to its original service. A newly
        # configured endpoint cannot receive a replay of a former payload.
        authentication = getattr(driver, 'authentication_fingerprint', None)
        if type(authentication) is not str or not re.fullmatch(r'[0-9a-f]{64}', authentication):
            raise MemoryServiceError('memory_service_unavailable', '记忆连接的认证摘要暂时无法核验。')
        value = {'connection': getattr(driver, 'connection_id', 'local/hindsight'),
                 'url': driver.base_url, 'token': authentication}
        native_scope = getattr(driver, 'source_fingerprint', None)
        if native_scope is not None:
            value['native_readonly_source'] = native_scope
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _get_native_source(self):
        if not self._native_source_enabled or not self._memory_enabled:
            raise MemoryServiceError('memory_service_unavailable', '原有Hermes记忆连接尚未启用。')
        try:
            return validate_native_source(self._native_source_getter())
        except (ValueError, OSError, TypeError, KeyError):
            raise MemoryServiceError('memory_service_unavailable', '原有Hermes记忆连接配置暂时无法核验。') from None

    def _native_transport(self, source):
        if self._native_transport_factory is not None:
            return self._native_transport_factory(source)
        return HindsightMemory(source['api_url'])

    def _memory_driver_for_profile(self, profile):
        binding = self.native_memory_bindings.get(profile['id'])
        program = profile.get('program', 'hindsight')
        if program != 'hindsight':
            if binding is not None:
                raise MemoryServiceError('memory_connection_changed', '原记忆绑定与程序不符，没有自动换库。')
            driver = self._memory_program_drivers.get(program)
            if driver is None:
                raise MemoryServiceError('memory_service_unavailable', '这份记忆程序尚未配置，原选择保留。')
            return driver
        if binding is None:
            return self._memory_driver()
        current = self._get_native_source()
        if current['source_fingerprint'] != binding['source_fingerprint']:
            raise MemoryServiceError('memory_connection_changed', '原Hermes连接已变化，旧绑定保留，没有自动换库。')
        return NativeHindsightReadOnly(binding, source_getter=self._get_native_source,
                                      transport=self._native_transport(current))

    def _memory_fingerprint_for_snapshot(self, snapshot):
        memory = snapshot['memory_profile'] if snapshot else {'id': 'none'}
        return self._memory_fingerprint_for_profile(memory)

    def _memory_fingerprint_for_profile(self, memory):
        return self._memory_fingerprint(self._memory_driver_for_profile(memory))

    @staticmethod
    def _native_source_ref(source):
        # Browser metadata identifies the currently inspected source without
        # publishing its private URL, bank ID or connection fingerprint. It is
        # only a compare token, never a credential or part of a run snapshot.
        digest = hashlib.sha256(('native-memory-source:' + source['source_fingerprint']).encode()).hexdigest()
        return 'nativesrc_' + digest[:32]

    def _native_source_inventory(self, connection):
        if not self._native_source_enabled:
            return []
        result = {'id': 'hermes_hindsight_current', 'name': 'Hermes 原有记忆', 'available': False,
                  'reason': '原有记忆连接暂不可用。', 'facts': None, 'documents': None, 'linked_profile_id': None,
                  'source_ref': None}
        try:
            source = self._get_native_source()
            result['source_ref'] = self._native_source_ref(source)
            linked = self.native_memory_bindings.find_linked(source)
            result['linked_profile_id'] = linked['id'] if linked else None
            if not connection['available']:
                return [result]
            key = ('native-source', source['source_fingerprint'])
            with self._memory_cache_lock:
                old = self._memory_cache.get(key)
            if old and time.monotonic() - old[0] < 30:
                state = old[1]
            else:
                state = probe_native_source(source, source_getter=self._get_native_source,
                                            transport=self._native_transport(source))
                with self._memory_cache_lock:
                    self._memory_cache[key] = (time.monotonic(), state)
            result.update(available=True, reason='', facts=state['facts'], documents=state['documents'])
        except (MemoryServiceError, ValueError, OSError, KeyError, TypeError):
            pass
        return [result]

    @admitted_service
    def attach_native_memory(self, body):
        fields = self._profile_write_fields(body, ('source_ref',))
        try:
            source = self._get_native_source()
            if 'source_ref' in fields:
                expected = fields['source_ref']
                if (not isinstance(expected, str) or not re.fullmatch(r'nativesrc_[0-9a-f]{32}', expected)
                        or expected != self._native_source_ref(source)):
                    raise ValueError('原有记忆来源已变化，请刷新管理页并核对后再接入。')
            probe_native_source(source, source_getter=self._get_native_source,
                                transport=self._native_transport(source), fresh=True)
            if self._get_native_source() != source:
                raise MemoryServiceError('memory_native_source_changed', '原有记忆连接在检查后变化，没有接到另一库。')
            # Local binding is immutable. Never create, rename or write the
            # physical Hermes bank, and never select this profile automatically.
            profile, created = self.native_memory_bindings.attach(source,
                description='只读Hermes原有记忆内容；Hermes仍可按原设置更新，本入口不提交保存。')
            with self._memory_cache_lock:
                self._memory_cache.clear()
            return {'profile': self._public_memory_profile(profile, fresh=True), 'created': created}
        except (MemoryServiceError, OSError):
            raise ValueError('原有Hermes记忆目前无法接入；没有创建替代空库或改变原库。') from None

    def _memory_connection(self):
        result = {'id': 'local/hindsight', 'program': 'hindsight', 'label': '这台 Mac 的 Hindsight',
                  'available': False, 'reason': '连接暂不可用；配置和草稿仍保留。',
                  'processing_notice': '记忆库存放在这台 Mac；保存新文字会使用本机 Hindsight 已配置的提取模型，可能经由外部模型服务。不会自动搬入整段旧聊天。'}
        try:
            driver = self._memory_driver()
            fingerprint = self._memory_fingerprint(driver)
            with self._memory_cache_lock:
                old = self._memory_cache.get(('health', fingerprint))
            if old and time.monotonic() - old[0] < 30:
                health = old[1]
            else:
                try:
                    health = driver.health()
                except (MemoryServiceError, ValueError, OSError):
                    health = {'available': False, 'write_supported': False}
                with self._memory_cache_lock:
                    self._memory_cache[('health', fingerprint)] = (time.monotonic(), health)
            if health.get('available') is True:
                result.update(available=True, reason='', write_supported=health.get('write_supported') is True)
        except (MemoryServiceError, ValueError, OSError):
            pass
        return result

    def _memory_program_connection(self, program):
        if program == 'hindsight':
            return self._memory_connection()
        connection_id, label = {
            'hermes_files': ('local/hermes-files', 'Hermes 文档记忆'),
            'mem0': ('local/mem0', 'Mem0'),
        }[program]
        result = dict(id=connection_id, program=program, label=label, available=False,
                      write_supported=False, reason='尚未配置这份记忆程序。')
        driver = self._memory_program_drivers.get(program)
        if driver is None:
            return result
        try:
            fingerprint = self._memory_fingerprint(driver)
            key = ('health', fingerprint)
            with self._memory_cache_lock:
                old = self._memory_cache.get(key)
            if old and time.monotonic() - old[0] < 30:
                health = old[1]
            else:
                health = driver.health()
                with self._memory_cache_lock:
                    self._memory_cache[key] = (time.monotonic(), health)
            if health.get('available') is True:
                result.update(available=True, reason='')
            elif isinstance(health.get('reason'), str):
                # Only local fixed diagnostics; never expose a private config.
                result['reason'] = ('Mem0 需要配置模型、嵌入服务和独立运行环境。'
                                    if program == 'mem0' else 'Hermes 文件核心暂不可用。')
        except (MemoryServiceError, ValueError, OSError):
            pass
        return result

    def _public_memory_profile(self, profile, connection=None, *, fresh=False):
        result = dict(make_memory_snapshot(profile), available=False, bank_state='unknown', reason='',
                      connection_label={'hermes_files': 'Hermes 文档记忆', 'mem0': 'Mem0'}.get(
                          profile['program'], '这台 Mac 的 Hindsight'), bank_label='独立记忆范围',
                      created_at=profile.get('created_at'), updated_at=profile.get('updated_at'))
        binding = self.native_memory_bindings.get(profile['id'])
        if binding:
            result.update(source_kind='hermes_hindsight_readonly', locked_mode=True,
                          native_source_ref=self._native_source_ref(binding),
                          bank_label='Hermes原有记忆（只读内容）')
        connection = (connection if connection and connection.get('program') == profile['program']
                      else self._memory_program_connection(profile['program']))
        if not connection['available']:
            return dict(result, bank_state='unavailable', reason=connection['reason'])
        if profile['mode'] == 'read_write' and connection.get('write_supported') is not True:
            return dict(result, bank_state='unavailable', reason='当前服务的保存协议尚未核验；配置仍保留。')
        try:
            driver = self._memory_driver_for_profile(profile)
            key = ('bank', self._memory_fingerprint(driver), profile['id'], profile['revision'])
            with self._memory_cache_lock:
                old = self._memory_cache.get(key)
            if old and not fresh and time.monotonic() - old[0] < 30:
                state = old[1]
            else:
                state = driver.bank_state(profile, fresh=fresh)
                with self._memory_cache_lock:
                    if len(self._memory_cache) > 256:
                        self._memory_cache.clear()
                    self._memory_cache[key] = (time.monotonic(), state)
            result.update(available=True, bank_state=state['state'])
        except (MemoryServiceError, ValueError, OSError, KeyError):
            result.update(bank_state='unavailable', reason=('原Hermes来源目前无法核验，旧绑定保留；不会自动换库。'
                if binding else '这份记忆库暂时无法核验。配置仍保留；保存配置可再次连接。'))
        return result

    def _provision_memory(self, profile):
        # Explicit creation/copy/save is the only bank provisioning action.
        # A transport failure still returns the durable profile, unavailable,
        # so the editor does not fabricate a second namespace on a retry.
        try:
            if self.native_memory_bindings.get(profile['id']) is None:
                self._memory_driver_for_profile(profile).ensure_bank(profile)
        except (MemoryServiceError, ValueError, OSError):
            pass
        with self._memory_cache_lock:
            self._memory_cache.clear()
        return self._public_memory_profile(profile, fresh=True)

    @admitted_service
    def create_memory_profile(self, body):
        fields = self._profile_write_fields(body, MUTABLE_FIELDS + ('program', 'connection_id'))
        if 'name' not in fields:
            raise ValueError('请填写记忆配置名称。')
        return {'profile': self._provision_memory(self.memory_profiles.create(**fields))}

    @admitted_service
    def update_memory_profile(self, profile_id, body, *, copying=False):
        fields = self._profile_write_fields(body, ('revision',) + MUTABLE_FIELDS + ('program', 'connection_id'))
        revision = fields.pop('revision', None)
        # A program or connection is a fixed reference, never a new endpoint.
        original = self.memory_profiles.get(profile_id)
        if (fields.pop('program', original['program']) != original['program'] or
                fields.pop('connection_id', original['connection_id']) != original['connection_id']):
            raise ValueError('更换记忆程序请新建配置，原记忆不会自动迁移。')
        writer = self.memory_profiles.copy if copying else self.memory_profiles.update
        if not copying and self.native_memory_bindings.get(profile_id) is not None and fields.get('mode', 'read_only') != 'read_only':
            raise ValueError('原有Hermes记忆在这里固定只读取；要保存新信息，请新建独立记忆配置。')
        try:
            return {'profile': self._provision_memory(writer(profile_id, revision, **fields))}
        except sqlite3.IntegrityError:
            raise ValueError('记忆来源绑定不允许这项修改，原配置和原库保留。') from None

    @admitted_service
    def profile_inventory(self):
        saved = self.profiles.saved_state()
        connection = self._memory_connection()
        notice = self.profile_migration_notice
        if saved.get('default_unavailable') and not notice:
            notice = '原默认 AI 配置暂不可读取，原选择保留；请从已有配置中选择。'
        return {'workspace_id': self.workspace_id,
                'ai_profiles': [self._public_ai_profile(profile) for profile in saved['profiles']],
                'native_profile_capabilities': self._native_profile_capabilities(),
                'memory_profiles': [{'id': 'none', 'name': '不使用长期记忆', 'available': True,
                    'detail': '小白兔不查询或写入长期记忆；当前聊天上文照常带入。已接入的文字连接隔离自动记忆。'}] +
                    [self._public_memory_profile(profile, connection) for profile in self.memory_profiles.list()['profiles']],
                'memory_connections': [connection] + [self._memory_program_connection(program)
                    for program in ('hermes_files', 'mem0') if program in self._memory_program_drivers],
                'native_memory_sources': self._native_source_inventory(connection),
                'default_ai_profile_id': saved['default_state']['ai_profile_id'],
                'default_ai_profile_revision': saved['default_state']['revision'], 'default_memory_profile_id': 'none',
                'revision': self.profiles_revision(saved=saved), 'notice': notice}

    @admitted_service
    def set_default_profile(self, body):
        fields = self._profile_write_fields(body, ('ai_profile_id', 'default_revision'))
        app_management.set_default_ai_profile(self.profiles, fields.get('ai_profile_id'),
                                             revision=fields.get('default_revision', PROFILE_UNSET))
        self._resolve_default_profile_notice()
        return self.profile_inventory()

    def _resolve_default_profile_notice(self):
        if self.profile_migration_notice in (
                '原默认 Agent 当前未接入，原选择保留；请从已有 AI 配置中选择。',
                '原默认 AI 配置暂不可读取，原选择保留；请从已有配置中选择。'):
            self.profile_migration_notice = ''  # The explicit choice resolves only these notices.

    @staticmethod
    def _profile_write_fields(body, allowed):
        if not isinstance(body, dict) or set(body) - (set(allowed) | {'_one_ai_csrf'}):
            raise ValueError('配置字段不在允许的清单内。')
        return {key: value for key, value in body.items() if key != '_one_ai_csrf'}

    @admitted_service
    def create_ai_profile(self, body):
        try:
            fields = self._profile_write_fields(body, PROFILE_FIELDS + ('creation_id',))
            if not {'name', 'agent_instance_id'}.issubset(fields):
                raise ValueError('请填写配置名称并选择已接入的 Agent。')
            creation_id = fields.pop('creation_id', None)
            saved = app_management.create_ai_profile(self.profiles, fields, creation_id=creation_id)
        except ProfileCreationConflict:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise ProfileCreationRejected(error) from None
        return {'profile': self._public_ai_profile(saved)}

    @admitted_service
    def update_ai_profile(self, profile_id, body, *, copying=False):
        try:
            fields = self._profile_write_fields(body, ('revision',) + PROFILE_FIELDS + (('creation_id',) if copying else ()))
            revision = fields.pop('revision', None)
            creation_id = fields.pop('creation_id', None)
            saved = app_management.edit_ai_profile(self.profiles, profile_id,
                revision, fields, copying=copying, creation_id=creation_id)
        except (ProfileConflict, ProfileCreationConflict):
            raise
        except (ValueError, TypeError, KeyError) as error:
            if copying:
                raise ProfileCreationRejected(error) from None
            raise
        return {'profile': self._public_ai_profile(saved)}

    @admitted_service
    def profile_selection(self, chat_id, *, metadata_only=False):
        selection = self.store.get_profile_selection(chat_id)
        if metadata_only:
            return {'workspace_id': self.workspace_id, 'scope': 'chat_configuration',
                    'chat_id': chat_id, 'profile_selection': selection,
                    'inference_performed': False, 'connection_checked': False}
        profile_id = selection['ai_profile_id']
        return {'profile_selection': selection,
                'ai_profile': self._public_ai_profile(self.profiles.get(profile_id)) if profile_id else None,
                'memory_profile': self._public_memory_profile(self.memory_profiles.get(selection['memory_profile_id']))
                    if selection['memory_profile_id'] != 'none' else {'id': 'none', 'name': '不使用长期记忆', 'available': True}}

    @admitted_service
    def set_profile_selection(self, chat_id, body, *, metadata_only=False):
        fields = self._profile_write_fields(body, ('revision', 'ai_profile_id', 'memory_profile_id'))
        revision = fields.pop('revision', None)
        if not ('ai_profile_id' in fields or 'memory_profile_id' in fields):
            raise ValueError('请选择需要更换的配置。')
        self.store.set_profile_selection(chat_id, revision=revision, **fields)
        return self.profile_selection(chat_id, metadata_only=metadata_only)

    @staticmethod
    def _accepts_keyword(runner, name):
        if not callable(runner):
            return False
        try:
            parameters = inspect.signature(runner).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(item.kind == inspect.Parameter.VAR_KEYWORD or
                   item.name == name and item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                       inspect.Parameter.KEYWORD_ONLY) for item in parameters)

    def _profile_snapshot_request(self, body):
        keys = {'ai_profile_id', 'ai_profile_revision', 'memory_profile_id', 'memory_profile_revision'}
        if not keys.intersection(body):
            return None  # Older clients retain their exact legacy route.
        if not {'ai_profile_id', 'ai_profile_revision', 'memory_profile_id'}.issubset(body):
            raise ValueError('请选择完整的 AI 配置和记忆配置。')
        if type(body['ai_profile_revision']) is not int:
            raise ValueError('AI 配置版本需要有效整数。')
        profile = self.profiles.get(body['ai_profile_id'])
        if body['ai_profile_revision'] != profile['revision']:
            raise ProfileConflict(profile)
        memory_id = body['memory_profile_id']
        if memory_id == 'none':
            if body.get('memory_profile_revision') is not None:
                raise ValueError('不使用长期记忆没有配置版本。')
            memory = {'id': 'none'}
        else:
            revision = validate_memory_profile_revision(body.get('memory_profile_revision'))
            memory = self.memory_profiles.get(memory_id)
            if revision != memory['revision']:
                raise MemoryProfileConflict(memory)
        backend = profile['agent_instance_id'].split('/', 1)[1]
        if ('backend' in body and body['backend'] != backend or
                'model_id' in body and body['model_id'] != profile['model_id']):
            raise ValueError('请求中的模型或 Agent 与所选 AI 配置不一致。')
        return make_profile_snapshot(profile, memory, schema_version=2)

    def _profile_request(self, body):
        snapshot = self._profile_snapshot_request(body)
        return (self._resolve_ai_profile(snapshot['ai_profile']), snapshot) if snapshot else (None, None)

    def _recall_memory(self, snapshot, text):
        if snapshot is None or snapshot['memory_profile']['id'] == 'none':
            return None, None
        try:
            memory = validate_request_snapshot(snapshot)['memory_profile']
        except (ValueError, KeyError, TypeError):
            raise MemoryRecallFailed() from None
        return self._recall_memory_profile(memory, text)

    def _recall_memory_profile(self, memory, text):
        """Recall for a real memory choice, independently of an AI profile.

        Shared-chat snapshots and native preparation reuse the same source,
        masking and capacity rules. The caller owns selection and receipts.
        """
        try:
            driver = self._memory_driver_for_profile(memory)
            fingerprint = self._memory_fingerprint(driver)
            if memory['mode'] == 'read_write' and driver.health().get('write_supported') is not True:
                raise MemoryServiceError('memory_write_protocol_unverified', '保存协议尚未核验。')
            query = memory_query(text)
            package = validate_recall_package(driver.recall(memory, query), memory, query=query)
            # Preserve bounded evidence, with the same local credential masking
            # used for ordinary transcripts. Rebind its hashes after masking.
            facts = []
            for original in package['facts']:
                fact = dict(original, text=INDEXER.redact(original['text']))
                if not fact['text'].strip():
                    continue
                fact['text_sha256'] = hashlib.sha256(fact['text'].encode()).hexdigest()
                identity = 'rabbit-' + memory['id'] + '\0' + fact['native_id'] + '\0' + fact['text']
                fact['reference_id'] = 'memref_' + hashlib.sha256(identity.encode()).hexdigest()[:32]
                facts.append(fact)
            package.update(facts=facts, omitted_results=package['total_results'] - len(facts),
                           characters=sum(len(fact['text']) for fact in facts))
            package = validate_recall_package(package, memory, query=query)
            selected_memory_block(package, memory)  # Reject capacity before any acceptance.
            if self._memory_fingerprint_for_profile(memory) != fingerprint:
                raise MemoryServiceError('memory_connection_changed', '记忆连接刚刚变化。')
            return package, fingerprint
        except (MemoryServiceError, ValueError, OSError, KeyError, TypeError):
            raise MemoryRecallFailed() from None

    def _memory_loop(self):
        while not self._memory_stop.is_set():
            try:
                self.memory_runner.tick(max_jobs=2)
            except Exception:
                # Durable jobs remain pollable; raw private transport errors
                # are never written into ordinary answers or service logs.
                pass
            self._memory_wake.wait(5)
            self._memory_wake.clear()

    def _memory_revision(self, chat_id):
        return self.memory_jobs.revision_for_chat(chat_id)

    def _public_memory_run(self, run):
        if (run and self.memory_runner and run.get('memory_info')
                and run['memory_info']['retain']['state'] == 'completed'):
            try:
                receipt = self.memory_runner.get_receipt(run['id'])
            except (KeyError, ValueError):
                receipt = None
            if receipt:
                run['memory_info']['retain'].update(receipt)
        return run

    @admitted_service
    def update_preferences(self, patch):
        if isinstance(patch, dict) and 'default_models' in patch:
            models = patch['default_models']
            if not isinstance(models, dict) or set(models) != set(AGENT_IDS):
                raise ValueError('默认模型选择无效。')
            for backend, model_id in models.items():
                validate_model_id(model_id, backend)
                if model_id:
                    self.model_catalog.resolve(backend, model_id)
        return self.preferences.update(patch)

    @admitted_service
    def managed_settings(self):
        return dict(app_settings.read(self.preferences), workspace_id=self.workspace_id)

    @admitted_service
    def update_managed_settings(self, body):
        return dict(app_settings.update(self.preferences, body), workspace_id=self.workspace_id)

    @admitted_service
    def local_document(self, body):
        path = body.get('path')
        try:
            result = read_local_document(self.documents_dir, path)
        except DocumentAccessDenied:
            result = read_document_snapshot(self.documents_dir, path, self.documents_cache_dir)
        else:
            result = dict(result, snapshot=False, updated_at=None)
        _, relative = document_path(self.documents_dir, path)
        ticket = self.document_tickets.issue(result, relative.as_posix())
        return dict(result, snapshot_ticket=ticket)

    def _file_source(self, material_id, *, content=False):
        try:
            return (self.file_tasks.source_content(material_id) if content
                    else self.file_tasks.get_source(material_id))
        except FileTaskStoreError as error:
            if error.code == 'not_found':
                raise KeyError('这份文件输入暂时找不到，没有改取其他文件。') from None
            raise

    @admitted_service
    def import_file_material(self, body, *, project_preview=False, markdown_preview=False):
        with self.worker_lock:
            if self._memory_stop.is_set():
                raise BusyError('服务正在停止，请稍后查询原上传回执。')
            result = self._import_file_material(body, project_preview=project_preview,
                                                markdown_preview=markdown_preview)
            if not result['material']['available']:
                self._wake_source_repair()
            return result

    def _import_file_material(self, body, *, project_preview=False, markdown_preview=False):
        client_id = body.get('client_id')
        # A lost response is recoverable after ticket expiry, restart, or a
        # changed payload. Never replace the originally accepted source.
        previous = self.file_tasks.find_source_receipt(client_id)
        if previous['received']:
            return {'material': previous['material']}
        allowed = {'client_id', 'ticket', '_one_ai_csrf'} if project_preview else {
            'client_id', 'name', 'data_base64', '_one_ai_csrf'}
        if set(body) - allowed:
            raise ValueError('文件输入参数不在允许的清单内。')
        if project_preview:
            seen = self.document_tickets.get(body.get('ticket'))
            origin = seen['provenance']
            if (markdown_preview and not (origin.get('kind') == 'upload' and 'from_markdown' in origin)
                    or not markdown_preview and origin.get('kind') != 'project_preview'):
                raise ValueError('文件预览凭证不属于所选来源，请重新预览。')
            material = self.file_tasks.add_source(seen['name'], seen['text'], seen['provenance'], client_id)
        else:
            encoded = body.get('data_base64')
            if not isinstance(encoded, str) or len(encoded) > ((MAX_DOCUMENT_BYTES + 2) // 3) * 4:
                raise ValueError('一份文本文件最多512 KiB，没有截断后添加。')
            try:
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise ValueError
                text = raw.decode('utf-8')
            except (ValueError, UnicodeError):
                raise ValueError('请选择512 KiB以内、UTF-8编码的.md或.txt文件；没有改写原文件。') from None
            material = self.file_tasks.add_source(body.get('name'), text, {'kind': 'upload'}, client_id)
        return {'material': material}

    @admitted_service
    def import_artifact_material(self, body):
        with self.worker_lock:
            if self._memory_stop.is_set():
                raise BusyError('服务正在停止，请稍后查询原导入回执。')
            client_id = body.get('client_id')
            previous = self.file_tasks.find_source_receipt(client_id)
            if previous['received']:
                material = previous['material']
            else:
                if set(body) - {'client_id', 'source_chat_id', 'artifact_id', 'sha256', '_one_ai_csrf'}:
                    raise ValueError('成果导入参数不在允许的清单内。')
                source_chat_id = body.get('source_chat_id')
                if (not isinstance(source_chat_id, str)
                        or re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', source_chat_id) is None):
                    raise ValueError('所选成果的原聊天标识无效。')
                if self.store.changes(source_chat_id)['selected'] is None:
                    raise KeyError('找不到这段对话。')
                try:
                    material = self.file_tasks.import_artifact_source(client_id,
                        source_chat_id=source_chat_id, artifact_id=body.get('artifact_id'),
                        expected_sha256=body.get('sha256'))
                except FileTaskStoreError as error:
                    if error.code == 'not_found':
                        raise KeyError('这份生成成果不属于所选聊天，或暂时找不到。') from None
                    raise
            if not material['available']:
                self._wake_source_repair()
            return {'material': material}

    def _publication_store(self):
        if self.publications is None:
            raise ValueError('这个入口尚未登记可另存成果的原项目。')
        return self.publications

    def _publication_revision(self, chat_id):
        with self.publication_lock:
            return self.publications.revision(chat_id) if self.publications is not None else 'disabled'

    @admitted_service
    def prepare_project_publication(self, chat_id, artifact_id, body):
        if set(body) - {'target_path', '_one_ai_csrf'}:
            raise ValueError('项目另存参数不在允许清单内。')
        with self.publication_lock:
            if self._memory_stop.is_set():
                raise BusyError('服务正在停止，原成果与草稿保留。')
            publications = self._publication_store()
            self.store.get(chat_id)
            artifact = self.file_tasks.get_artifact(artifact_id, chat_id=chat_id)
            # get_artifact also resolves workspace inputs. Only actual
            # generated, chat-owned, verified artifacts can be published.
            if not artifact.get('run_id') or artifact.get('chat_id') != chat_id:
                raise ValueError('请从这段聊天实际保存的生成成果进入原项目另存。')
            task = self.file_tasks.get_task(artifact['run_id'], chat_id=chat_id)
            if not any(item['id'] == artifact_id and item['available'] and item['content_verified']
                       for item in task['artifacts']):
                raise ValueError('这份生成成果暂未通过完整内容核验。')
            content = self.file_tasks.artifact_content(artifact_id, chat_id=chat_id)
            return publications.prepare(content['artifact'], content['text'], body.get('target_path'))

    @admitted_service
    def confirm_project_publication(self, publication_id, body):
        with self.publication_lock:
            publications = self._publication_store()
            previous = publications.find_receipt(body.get('client_id'))
            if previous['received']:
                return previous
            if set(body) - {'client_id', '_one_ai_csrf'}:
                raise ValueError('项目提交参数不在允许清单内。')
            if self._memory_stop.is_set():
                raise BusyError('服务正在停止，请稍后查询原项目提交回执。')
            return publications.confirm(publication_id, body.get('client_id'))

    @admitted_service
    def project_publication_receipt(self, client_id):
        with self.publication_lock:
            return self._publication_store().find_receipt(client_id)

    @admitted_service
    def project_publication(self, publication_id):
        with self.publication_lock:
            return {'publication': self._publication_store().get(publication_id)}

    def _markdown_registry(self):
        if self.markdown_chats is None:
            raise ValueError('这个入口还没有可用的对话文件夹。')
        return self.markdown_chats

    @admitted_service
    def list_markdown_chats(self):
        with self.publication_lock:
            registry = self._markdown_registry()
            bindings = registry.bindings()
            current = {}
            native = NativeMarkdownSource(self.store, self.history, self.library)
            for binding in bindings:
                source = binding['source']
                identity = markdown_source_identity(source)
                if source['kind'] == 'native_index':
                    if not self.refresh_lock.acquire(blocking=False):
                        raise BusyError('历史正在更新，请稍后重新读取文件夹。')
                    try:
                        current[identity] = native_revision(native.snapshot(source['agent'], source['chat_id']))
                    except (KeyError, ValueError, OSError, sqlite3.Error):
                        # Keep accepted files visible when one current index
                        # is absent, over capacity or changing. None means
                        # unverifiable here, never authority to delete it.
                        current[identity] = None
                    finally:
                        self.refresh_lock.release()
                elif source['kind'] == 'history_reference':
                    try:
                        HistoryMarkdownReference(self.history, self.workspace_id).get(source['agent'], source['session_id'])
                        current[identity] = source['revision']
                    except (KeyError, ValueError, OSError, sqlite3.Error):
                        current[identity] = None
                else:
                    selected = self.store.changes(source['chat_id'])['selected']
                    current[identity] = selected['revision'] if selected is not None else None
            # A bounded directory observation, not a filesystem transaction or
            # authority to replace an externally edited document.
            candidates = self.store.list()['chats']
            native_candidates = native.candidates()
            return dict(registry.scan(current), root=str(self.markdown_root),
                publications=registry.list(limit=1000),
                bindings=bindings,
                source_chats=[{key: row[key] for key in ('id', 'title', 'revision', 'running')}
                    for row in candidates[:1000]], source_chats_total=len(candidates),
                source_chats_truncated=len(candidates) > 1000,
                native_source_chats=native_candidates[:1000],
                native_source_chats_total=len(native_candidates),
                native_source_chats_truncated=len(native_candidates) > 1000)

    @admitted_service
    def create_markdown_folder(self, body):
        if type(body) is not dict or set(body) - {'path', '_one_ai_csrf'} or 'path' not in body:
            raise ValueError('请填写分组名称。')
        with self.publication_lock:
            folder = self._markdown_registry().directory.create_folder(body['path'])
            return dict(workspace_id=self.workspace_id, folder=folder)

    @admitted_service
    def move_markdown_file(self, body):
        fields = {'binding_id', 'expected_head_id', 'source_path', 'source_sha256', 'target_folder'}
        if type(body) is not dict or set(body) - fields - {'_one_ai_csrf'} or not fields <= body.keys():
            raise MarkdownMoveError('invalid_arguments')
        with self.publication_lock:
            result = self._markdown_registry().move_file(**{key: body[key] for key in fields})
            return dict(result, workspace_id=self.workspace_id)

    @admitted_service
    def open_markdown_folder(self, body):
        if type(body) is not dict or set(body) - {'_one_ai_csrf'}:
            raise ValueError('只能打开当前对话文件夹。')
        if sys.platform != 'darwin':
            raise ValueError('当前平台尚未接入文件夹打开功能。')
        with self.publication_lock, self._markdown_registry().directory.root_scope():
            subprocess.run(['/usr/bin/open', '-a', 'Finder', str(self.markdown_root)],
                check=True, timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dict(workspace_id=self.workspace_id, opened=True)

    @admitted_service
    def history_markdown_reference(self, agent, session_id):
        with self.publication_lock:
            self._markdown_registry()
            value = HistoryMarkdownReference(self.history, self.workspace_id).get(agent, session_id)
            return dict(workspace_id=self.workspace_id, reference=value)

    @admitted_service
    def prepare_history_markdown_reference(self, body):
        if (type(body) is not dict or set(body) - {'agent', 'session_id', 'target_path', '_one_ai_csrf'}
                or not all(key in body for key in ('agent', 'session_id', 'target_path'))):
            raise ValueError('整理索引需要 Agent、原会话编号和 Markdown 相对路径。')
        with self.publication_lock:
            registry = self._markdown_registry()
            document = HistoryMarkdownReference(self.history, self.workspace_id).render(body['agent'], body['session_id'])
            return dict(registry.prepare(document, body['target_path']), workspace_id=self.workspace_id)

    @admitted_service
    def prepare_native_markdown_chat(self, body):
        if (type(body) is not dict or set(body) - {'agent', 'chat_id', 'target_path', '_one_ai_csrf'}
                or not all(key in body for key in ('agent', 'chat_id', 'target_path'))
                or type(body['target_path']) is not str or not body['target_path'].casefold().endswith('.md')):
            raise ValueError('原生整理需要 Agent、原对话编号和 Markdown 相对路径。')
        with self.publication_lock:
            registry = self._markdown_registry()
            document = self._native_markdown_document(body['agent'], body['chat_id'])
            return dict(registry.prepare(document, body['target_path']), workspace_id=self.workspace_id)

    def _native_markdown_document(self, agent, chat_id):
        if not self.refresh_lock.acquire(blocking=False):
            raise BusyError('历史正在更新，请稍后重新预览。')
        try:
            snapshot = NativeMarkdownSource(self.store, self.history, self.library).snapshot(agent, chat_id)
            return render_native_markdown(snapshot, self.workspace_id)
        except (OSError, sqlite3.Error):
            raise HistoryIndexUnavailable('原生文字索引暂不可读取，原会话和文件保留。') from None
        finally:
            self.refresh_lock.release()

    @admitted_service
    def markdown_file_preview(self, body):
        if type(body) is not dict or set(body) - {'binding_id', 'expected_head_id', '_one_ai_csrf'}:
            raise ValueError('Markdown资料预览参数无效。')
        with self.publication_lock:
            document = self._markdown_registry().current_file(body.get('binding_id'),
                                                              body.get('expected_head_id'))
            if document['source']['kind'] == 'history_reference':
                raise ValueError('索引引用不包含聊天正文，请从原对话选择资料。')
            ticket = self.document_tickets.issue_markdown(document)
            return dict(document, workspace_id=self.workspace_id, snapshot_ticket=ticket)

    @admitted_service
    def prepare_markdown_update(self, body):
        if (type(body) is not dict or set(body) - {'binding_id', 'expected_head_id', '_one_ai_csrf'}
                or not all(key in body for key in ('binding_id', 'expected_head_id'))):
            raise ValueError('更新快照需要原绑定和刚读取的版本编号，不接受新路径或正文。')
        with self.publication_lock:
            sync = self._markdown_registry().sync
            binding = sync.binding(body['binding_id'])
            source = binding['source']
            if source['kind'] == 'history_reference':
                raise ValueError('索引引用不复制正文，无需更新快照。')
            if source['kind'] == 'native_index':
                document = self._native_markdown_document(source['agent'], source['chat_id'])
            else:
                document = render_markdown_chat(self.store.markdown_snapshot(source['chat_id']), self.workspace_id)
            return dict(sync.prepare(body['binding_id'], body['expected_head_id'], document),
                workspace_id=self.workspace_id)

    @admitted_service
    def markdown_update_preview(self, update_id):
        with self.publication_lock:
            return dict(self._markdown_registry().sync.preview(update_id), workspace_id=self.workspace_id)

    @admitted_service
    def confirm_markdown_update(self, update_id, body):
        if type(body) is not dict or set(body) - {'client_id', '_one_ai_csrf'}:
            raise ValueError('更新确认只接受原请求编号。')
        with self.publication_lock:
            return dict(self._markdown_registry().sync.confirm(update_id, body.get('client_id')),
                workspace_id=self.workspace_id)

    @admitted_service
    def markdown_update_receipt(self, client_id):
        with self.publication_lock:
            return dict(self._markdown_registry().sync.find_receipt(client_id), workspace_id=self.workspace_id)

    @admitted_service
    def prepare_markdown_chat(self, chat_id, body):
        if (type(body) is not dict or set(body) - {'source_revision', 'target_path', '_one_ai_csrf'}
                or type(body.get('source_revision')) is not str
                or re.fullmatch(r'0|[1-9][0-9]{0,18}', body['source_revision']) is None
                or type(body.get('target_path')) is not str
                or not body['target_path'].casefold().endswith('.md')):
            raise ValueError('整理需要原对话版本和对话文件夹内的 Markdown 相对路径。')
        with self.publication_lock:
            registry = self._markdown_registry()
            snapshot = self.store.markdown_snapshot(chat_id)
            if snapshot['revision'] != body['source_revision']:
                raise ValueError('对话已有变化，请重新打开后再核对；没有保存旧版本。')
            document = render_markdown_chat(snapshot, self.workspace_id)
            return dict(registry.prepare(document, body['target_path']), workspace_id=self.workspace_id)

    @admitted_service
    def confirm_markdown_chat(self, publication_id, body):
        if type(body) is not dict or set(body) - {'client_id', '_one_ai_csrf'}:
            raise ValueError('整理确认参数不在允许清单内。')
        with self.publication_lock:
            return dict(self._markdown_registry().confirm(publication_id, body.get('client_id')),
                workspace_id=self.workspace_id)

    @admitted_service
    def markdown_chat_receipt(self, client_id):
        with self.publication_lock:
            return dict(self._markdown_registry().find_receipt(client_id), workspace_id=self.workspace_id)

    @admitted_service
    def markdown_chat_preview(self, publication_id):
        with self.publication_lock:
            return dict(self._markdown_registry().preview(publication_id), workspace_id=self.workspace_id)

    @admitted_service
    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            raise BusyError('历史正在更新，请稍后再试。')
        try:
            stats = self.history_sources.refresh()
            self.history_notice = ''
            return {'ok': True, 'stats': stats}
        finally:
            self.refresh_lock.release()

    @staticmethod
    def _creation_key(body):
        creation_key = body.get('client_id')
        if creation_key is not None and (not isinstance(creation_key, str) or not 1 <= len(creation_key) <= 100):
            raise ValueError('创建请求标识无效。')
        return creation_key

    @staticmethod
    def _reuse_source(body):
        reuse = body.get('reuse_source', False)
        if type(reuse) is not bool:
            raise ValueError('历史接续选项无效。')
        return reuse

    @admitted_service
    def create(self, body):
        creation_key, reuse = self._creation_key(body), self._reuse_source(body)
        # Select/reuse/create as one local operation. A second entry cannot
        # create another source copy between this lookup and storage.
        with self.store.lock:
            if creation_key:
                previous = self.store.find_creation(creation_key)
                if previous:
                    return {'chat': previous}
            source, imported = body.get('source'), []
            title = body.get('title', '新对话')
            if not isinstance(title, str) or len(title) > 120:
                raise ValueError('对话标题最多 120 个字。')
            if source:
                self._validate_history_source(source)
                if reuse and self.store.find_source(source):
                    # ChatStore binds this new request key to the selected copy
                    # so an eventual retry still returns that copy, not a later
                    # independently created source branch.
                    return {'chat': self.store.create(title, source, creation_key=creation_key, reuse_source=True)}
                title, imported = self._read_history_source(source, title)
            elif reuse:
                raise ValueError('请选择需要接续的原历史。')
            return {'chat': self.store.create(title, source, imported, creation_key, reuse_source=reuse)}

    def _read_history_source(self, source, title='新对话'):
        """Read the complete indexed source identically for create and preview.

        Each page is read-only. Reject an index replacement/change between pages
        rather than combining records from different versions of the history.
        """
        self._validate_history_source(source)
        session_id = source['session_id']
        try:
            snapshot = self.index_path.stat()
        except OSError:
            raise HistoryIndexUnavailable('历史索引暂时无法读取，请先更新历史后再试。') from None
        imported, offset = [], 0
        while True:
            page = self.history.get_session(source['agent'], session_id, offset=offset, limit=100, for_context=True)
            if not page.get('session'):
                raise KeyError('找不到原来的历史对话。')
            if page['session'].get('coverage', {}).get('continuable') is False:
                raise HistorySourceIncomplete(page['session']['coverage'].get('notice') or '这份来源尚未通过完整性核对，不能整段接续。')
            if offset == 0 and title == '新对话':
                title = page['session']['title'] or '从旧聊天继续'
            imported.extend(dict(message, agent=source['agent'], session_id=session_id)
                            for message in page['messages'])
            if len(imported) > 20000:
                raise ValueError('这段历史太长，请先导出后选择需要继续的部分。')
            offset = page.get('next_offset')
            if offset is None:
                break
        try:
            latest = self.index_path.stat()
        except OSError:
            raise BusyError('历史刚刚更新了，请重新打开这段历史后再试。') from None
        if (snapshot.st_dev, snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns, snapshot.st_ctime_ns) != (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns):
            raise BusyError('历史刚刚更新了。请重新打开这段历史后再继续，避免复制出重复或缺失的消息。')
        return title, imported

    @staticmethod
    def _validate_history_source(source):
        if not isinstance(source, dict) or not registered_agent(source.get('agent')):
            raise ValueError('请选择一个已接入的历史来源。')
        session_id = source.get('session_id')
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
            raise ValueError('历史对话标识无效。')

    @admitted_service
    def get_chat(self, chat_id):
        from history_view import display_message
        # Capture before evidence reads. A job transition during the response
        # must remain visible to the next change poll rather than stamping old
        # evidence with its newer counter and leaving it stuck on screen.
        memory_revision = self._memory_revision(chat_id)
        file_revision = self.file_tasks.revision(chat_id)
        publication_revision = self._publication_revision(chat_id)
        result = self.store.get(chat_id)
        result['profile_selection'] = self.store.get_profile_selection(chat_id)
        snapshots = self.store.profile_message_snapshots(chat_id)
        request_configurations = self.store.message_request_configurations(chat_id)
        memory_evidence = self.store.memory_message_info(chat_id)
        memory_receipts = self.memory_runner.receipts_for_chat(chat_id) if self.memory_runner else {}
        file_receipts = self._file_receipts(chat_id)
        file_runs = self.store.file_task_runs(chat_id)
        inputs_by_message, materials = {}, {}
        for run in file_runs:
            selected = []
            for frozen in run['file_inputs']:
                try:
                    if run['task_intent']['mode'] == 'files':
                        material = self.file_tasks.get_source(frozen['id'])
                    else:
                        material = self.file_tasks.input_snapshot(frozen['id'], chat_id=chat_id,
                            expected_sha256=frozen['sha256'])['artifact']
                except FileTaskStoreError as error:
                    if run['task_intent']['mode'] == 'files':
                        if error.code == 'not_found':
                            continue
                        raise
                    ref = next((item for item in (run.get('context_info') or {}).get('file_input_refs', [])
                                if item.get('id') == frozen['id']), {})
                    material = {key: ref[key] for key in ('name', 'size_bytes', 'source') if key in ref}
                    material.update(frozen, path=ref.get('path', ref.get('name', '文件输入.txt')),
                        available=False, content_verified=False,
                        reason='not_found' if error.code == 'not_found' else 'changed')
                selected.append(material)
                if material.get('source') != 'generated':
                    materials[material['id']] = material
            if selected:
                inputs_by_message[run['user_message_id']] = selected
        result['file_materials'] = list(materials.values())
        with self.publication_lock:
            result['project_publications'] = self.publications.list(chat_id) if self.publications is not None else []
        for message in result['messages']:
            if message['id'] in inputs_by_message:
                message['file_inputs'] = inputs_by_message[message['id']]
            message.pop('text_file_inputs', None)
            message.pop('text_file_input_refs', None)
            if message['id'] in file_receipts:
                message['file_task'] = file_receipts[message['id']]
            if message['id'] in snapshots:
                message['profile_snapshot'] = snapshots[message['id']]
            if message['id'] in request_configurations:
                message['request_configuration'] = request_configurations[message['id']]
            if message['id'] in memory_evidence:
                message['memory_info'] = memory_evidence[message['id']]
                if (message['memory_info']['retain']['state'] == 'completed'
                        and message['id'] in memory_receipts):
                    message['memory_info']['retain'].update(memory_receipts[message['id']])
            if message.get('status') == 'imported':
                source = message.get('provenance') or {}
                agent = message.get('backend') or source.get('agent') or 'codex'
                message.update(display_message(message['content'], agent, message['role'], phase=message.get('phase') or ''))
        if result.get('run'):
            self._public_memory_run(result['run'])
            with self.worker_lock:
                worker = self.workers.get(result['run']['id'])
                result['run']['cancel_requested'] = bool(worker and worker[1].is_set())
        result['revision'] = self.instance_id + ':' + result['revision'] + ':' + memory_revision + ':' + file_revision + ':' + publication_revision
        return result

    @staticmethod
    def _source_update_plan(view, source, indexed=None):
        plan = {'status': 'no_source' if source is None else 'missing', 'source': source,
                'source_version': None, 'chat_revision': int(view['revision']),
                'indexed_messages': 0, 'imported_messages': 0, 'new_messages': 0,
                'records': [], 'records_total': 0}
        if source is not None:
            comparison = source_update_comparison(source, view['messages'], indexed or [])
            plan['imported_messages'] = comparison['imported_messages']
            if indexed is not None:
                plan.update(comparison)
                if plan['status'] == 'appendable':
                    native = [message for message in indexed if message.get('role') in ('user', 'assistant')]
                    tail = native[plan['imported_messages']:]
                    plan['records'] = [{'message_id': message.get('message_id'), 'role': message['role'],
                                        'timestamp': message.get('timestamp'), 'phase': message.get('phase') or '',
                                        'preview': message.get('content', '')[:180],
                                        'characters': len(message.get('content', ''))}
                                       for message in tail[:20]]
                    plan['records_total'] = len(tail)
        plan['notice'] = {
            'no_source': '这段共同聊天没有关联的原生来源。',
            'missing': '原生来源暂时无法读取；共同聊天副本保留，可更新历史后重新查看。',
            'unchanged': '原生来源没有新的文字消息。',
            'appendable': '可完整追加来源新增尾部。它会录入到共同聊天末尾；两个入口各自续聊的顺序不会自动合并。',
            'diverged': '原生来源已有删除、改动或顺序变化，已导入内容不再是完整前缀。请查看来源原文；本次不能自动追加。',
        }[plan['status']]
        return plan

    def _source_update_snapshot(self, chat_id):
        view = self.store.get(chat_id)
        saved_source = view['chat'].get('source')
        if not saved_source:
            return view, None, None, None
        # The persisted chat chooses its source. Request bodies cannot select a
        # different native session or manufacture provenance for this branch.
        source = {key: saved_source.get(key) for key in ('agent', 'session_id')}
        self._validate_history_source(source)
        try:
            snapshot = self.index_path.stat()
        except OSError:
            return view, source, None, None
        try:
            _title, indexed = self._read_history_source(source)
        except (KeyError, OSError, sqlite3.Error, HistoryIndexUnavailable, HistorySourceIncomplete):
            return view, source, None, None
        return view, source, indexed, (snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns)

    @admitted_service
    def source_update(self, chat_id):
        view, source, indexed, _signature = self._source_update_snapshot(chat_id)
        return {'source_update': self._source_update_plan(view, source, indexed)}

    @admitted_service
    def append_source_update(self, chat_id, body):
        if set(body) - {'source_version', 'chat_revision', '_one_ai_csrf'}:
            raise ValueError('来源更新只能使用已查看的来源版本和共同聊天版本。')
        version, revision = body.get('source_version'), body.get('chat_revision')
        self.store.validate_source_update(version, revision)
        view, source, indexed, signature = self._source_update_snapshot(chat_id)
        if source is None or indexed is None:
            raise SourceUpdateConflict('原生来源暂时不可用，请重新查看来源更新；未追加任何消息。')
        def source_guard():
            try:
                latest = self.index_path.stat()
            except OSError:
                raise SourceUpdateConflict('原生来源暂时不可用，请重新查看来源更新；未追加任何消息。') from None
            if signature != (latest.st_ino, latest.st_size, latest.st_mtime_ns):
                raise SourceUpdateConflict('历史索引刚刚变化，请重新查看来源更新再追加。')
        appended = self.store.append_source_update(chat_id, source, indexed, version, revision, source_guard)
        plan = self._source_update_plan(self.store.get(chat_id), source, indexed)
        if appended:
            plan['notice'] = '已完整录入来源新增尾部，未发送给 AI。原生时间保留在来源记录中；录入顺序不代表两个续聊分支的原生先后。'
        return {'source_update': plan, 'appended_messages': appended}

    @staticmethod
    def _source_images_plan(view, source, indexed=None):
        """Return a bounded review, without the full native body or locators."""
        base = {'source': source, 'status': 'no_source' if source is None else 'missing',
                'plan_version': None, 'chat_revision': int(view['revision']),
                'counts': {'backfilled_messages': 0, 'backfilled_images': 0, 'inserted_image_messages': 0},
                'records': [], 'records_total': 0,
                'notice': ('这段聊天没有关联的原生来源。' if source is None else
                           '原生来源暂时不可用；已保留的聊天和图片副本不会被覆盖。')}
        if source is None or indexed is None:
            return base
        plan = source_image_backfill_plan(source, view['messages'], indexed, chat_revision=int(view['revision']))
        base.update({key: plan[key] for key in ('status', 'plan_version', 'chat_revision', 'counts', 'notice')})
        native = {message['message_id']: message for message in indexed}
        records = [(update, native[update['source_message_id']], 'attach_images')
                   for update in plan['material_updates']]
        records.extend((message, message, 'insert_image_message') for message in plan['new_image_messages'])
        base['records_total'] = len(records)
        for update, message, action in records[:20]:
            descriptions = update.get('native_attachments', [])
            base['records'].append({
                'action': action, 'message_id': update.get('message_id') if action == 'attach_images' else None,
                'source_message_id': message['message_id'], 'role': message['role'],
                'timestamp': message.get('timestamp'), 'preview': message.get('content', '')[:180],
                'images': len(update.get('attachment_ids', [])),
                'unavailable': sum(item.get('kind') != 'inspection_summary' and item.get('material_state') != 'ready'
                                   for item in descriptions),
                'omitted': sum(max(0, item.get('omitted_count', 0)) for item in descriptions),
                'unknown_omitted': any(item.get('omitted_count', 0) < 0 for item in descriptions),
            })
        return base

    @admitted_service
    def source_images(self, chat_id):
        view, source, indexed, _signature = self._source_update_snapshot(chat_id)
        return {'source_images': self._source_images_plan(view, source, indexed)}

    @admitted_service
    def source_images_receipt(self, chat_id, version, raw_revision):
        if not isinstance(raw_revision, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,15})', raw_revision):
            raise ValueError('图片补入回执的聊天版本无效。')
        return {'receipt': self.store.find_source_image_backfill_receipt(chat_id, version, int(raw_revision))}

    @admitted_service
    def backfill_source_images(self, chat_id, body):
        if set(body) - {'plan_version', 'chat_revision', '_one_ai_csrf'}:
            raise ValueError('图片补入只能使用已查看的计划和聊天版本。')
        version, revision = body.get('plan_version'), body.get('chat_revision')
        self.store.validate_source_update(version, revision)
        view, source, indexed, signature = self._source_update_snapshot(chat_id)
        if source is None:
            raise SourceUpdateConflict('这段聊天没有关联的原生来源，不能补入图片。')
        def source_guard():
            if indexed is None or signature is None:
                raise SourceUpdateConflict('原生来源暂时不可用，请重新查看图片补入；未修改消息。')
            try:
                latest = self.index_path.stat()
            except OSError:
                raise SourceUpdateConflict('原生来源暂时不可用，请重新查看图片补入；未修改消息。') from None
            if signature != (latest.st_ino, latest.st_size, latest.st_mtime_ns):
                raise SourceUpdateConflict('历史索引刚刚变化，请重新查看图片补入。')
        # Receipt lookup in the store precedes source_guard, so a lost success
        # can still be recovered after a source disappears or later chat edits.
        result = self.store.backfill_source_images(chat_id, source, indexed or [], version, revision, source_guard)
        plan = self._source_images_plan(self.store.get(chat_id), source, indexed)
        return {'source_images': plan, **result}

    @admitted_service
    def context_pins(self, chat_id):
        result = self.store.get(chat_id)
        pins = result['context_selection']['pinned_ids']
        messages = self._clean_context_messages(result['messages'])
        return {'context_selection': result['context_selection'],
                'records': [{'message_id': message['id'], 'role': message['role'],
                             'source_ai': message.get('backend'), 'preview': message['content'][:180],
                             'characters': len(message['content']), 'pinned': True}
                            for message in messages if message['id'] in pins]}

    @admitted_service
    def set_context_pin(self, chat_id, body):
        # Reject transport-only imported records that have no usable text.
        if body.get('pinned') is True:
            messages = self._clean_context_messages(self.store.get(chat_id)['messages'])
            message = next((value for value in messages if value['id'] == body.get('message_id')), None)
            if (message is not None and not message['content'].strip() and not message.get('attachment_ids')
                    and not message.get('native_attachments')):
                raise ValueError('这条记录清理后没有可带入的对话正文。')
        return {'context_selection': self.store.set_context_pin(chat_id, body.get('message_id'),
                body.get('pinned'), body.get('revision'))}

    @admitted_service
    def changes(self, chat_id=None):
        result = self.store.changes(chat_id)
        result['profiles_revision'] = self.profiles_revision()
        result['library_revision'] = self.instance_id + ':' + self.library.revision(result['revision'])
        # Presentation preferences must not become a prerequisite for observing
        # a running/completed chat. Keep a damaged file intact and let its own
        # endpoint report the error; repaired preferences are noticed next poll.
        try:
            result['preferences_revision'] = str(self.preferences.read()['revision'])
        except ValueError as error:
            result['preferences_revision'] = None
            result['preferences_notice'] = str(error)
        result['revision'] = self.instance_id + ':' + result['revision']
        if result['selected']:
            result['selected']['revision'] = (self.instance_id + ':' + result['selected']['revision'] + ':' +
                                              self._memory_revision(chat_id) + ':' + self.file_tasks.revision(chat_id) + ':' +
                                              self._publication_revision(chat_id))
        return result

    @admitted_service
    def list_library(self, *, folder=None, **options):
        if folder is not None:
            directory = self.list_markdown_chats()
            if type(folder) is not str or folder not in directory['folders']:
                raise ValueError('找不到这个分组，请重新读取文件夹。')
            if not directory['complete']:
                raise ValueError('对话文件夹未读完整，请重新读取后再试。')
            bindings = {markdown_source_identity(item['source']): item['source'] for item in directory['bindings']}
            keys = []
            for row in directory['chats']:
                if (len(row['paths']) != 1 or not row.get('file')
                        or not (row['file']['folder'] == folder or row['file']['folder'].startswith(folder + '/'))):
                    continue
                if row.get('kind') == 'history_reference':
                    source = bindings.get(('history_reference', row['agent'], row['rabbit_id']))
                    if source is not None:
                        keys.append('history:' + source['agent'] + ':' + source['session_id'])
                else:
                    keys.append(('native:' + row['agent'] + ':' if row.get('kind') == 'native_index' else 'chat:') + row['rabbit_id'])
            options['keys'] = keys
        result = self.library.list(**options)
        result['revision'] = self.instance_id + ':' + result['revision']
        if 'base_revision' in result:
            result['base_revision'] = self.instance_id + ':' + result['base_revision']
        return result

    @admitted_service
    def receipt(self, kind, client_id, chat_id=None):
        if kind not in ('creation', 'message'):
            raise ValueError('请求回执类型无效。')
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError('请求标识无效。')
        if chat_id is not None and (not isinstance(chat_id, str) or not 1 <= len(chat_id) <= 256):
            raise ValueError('对话标识无效。')
        if kind == 'creation':
            chat = self.store.find_creation(client_id)
            return {'received': True, 'chat_id': chat['id']} if chat else {'received': False}
        if chat_id is None:
            raise ValueError('请提供消息所属的对话。')
        # Read only the chat's identity/counter, not its entire transcript. A
        # request remains identifiable even after newer runs have completed.
        if self.store.changes(chat_id)['selected'] is None:
            raise KeyError('找不到这段对话。')
        run = self.store.find_submission(chat_id, client_id)
        if run is None:
            return {'received': False}
        return {'received': True, 'chat_id': chat_id, 'run_id': run['id'],
                'status': run['status'], 'backend': run['backend']}

    @admitted_service
    def set_chat_backend(self, body):
        chat_id, backend = body.get('chat_id'), body.get('backend')
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
            raise ValueError('对话标识无效。')
        if not registered_agent(backend) or backend not in self.adapters:
            raise ValueError('请选择已接入的 AI 连接。')
        self.store.get(chat_id)
        model_id = body.get('model_id') if 'model_id' in body else None
        if 'model_id' in body:
            validate_model_id(model_id, backend)
            if model_id:
                self.model_catalog.resolve(backend, model_id)
        return self.preferences.set_chat_backend(chat_id, backend, model_id)

    def _task_request(self, body, chat_id):
        intent = validate_intent(body.get('task_intent'))
        if intent['mode'] == 'chat' and not intent['artifact_ids']:
            return intent, []
        if intent['mode'] == 'files' and body.get('attachment_ids'):
            raise ValueError('文件任务目前只支持文字；请移除本轮图片，再发送。')
        inputs = []
        for artifact_id in intent['artifact_ids']:
            content = self.file_content(chat_id, artifact_id) if chat_id is not None else self._file_source(artifact_id, content=True)
            raw = content['text'].encode('utf-8')
            digest = hashlib.sha256(raw).hexdigest()
            if digest != content['artifact']['sha256']:
                raise ValueError('所选文件成果的实际内容与保存版本不同，没有发给模型。')
            inputs.append({'id': artifact_id, 'sha256': digest})
        return intent, inputs

    def _require_file_route(self, route, task_intent):
        """Pure capability check; unknown preserves established file connectors."""
        if validate_intent(task_intent)['mode'] != 'files':
            return
        if not isinstance(route, dict) or route.get('backend') not in self.file_adapters:
            raise ValueError('这条 AI 连接暂不能执行文件任务，请保留草稿后选择已接入的连接。')
        if route.get('file_output') == 'unsupported':
            raise ValueError('所选模型的这条连接暂不支持文件任务；草稿与文件材料保留，可切回普通聊天或选择支持文件的 AI 配置。')

    def _file_input_refs(self, intent, chat_id):
        refs = []
        for number, material_id in enumerate(validate_intent(intent)['artifact_ids'], 1):
            material = (self.file_tasks.get_artifact(material_id, chat_id=chat_id) if chat_id is not None
                        else self._file_source(material_id))
            if not material.get('available') or not material.get('content_verified'):
                raise ValueError('有文件输入尚未核验或暂不可读，原草稿和选择保留。')
            refs.append({'id': material_id, 'name': material['name'], 'sha256': material['sha256'],
                'size_bytes': material['size_bytes'], 'source': material.get('source', 'generated'),
                'task_path': '输入-%02d-%s%s' % (number, material_id, Path(material['name']).suffix.casefold())})
            if intent['mode'] == 'chat':
                refs[-1]['path'] = material['path']
                if material.get('chat_id'):
                    refs[-1]['chat_id'] = material['chat_id']
        return refs

    @staticmethod
    def _validate_text_file_input_refs(intent, refs, frozen_inputs):
        """Bind the current read projection to its already frozen receipt."""
        intent = validate_intent(intent)
        if intent['mode'] != 'chat':
            return  # Generated files retain their existing prepare guard.
        if not isinstance(refs, list) or any(not isinstance(item, dict) for item in refs):
            raise ValueError('本轮文本材料目录与冻结的输入不同，没有发给模型。')
        expected = validate_inputs(frozen_inputs, intent)
        projected = validate_inputs([{'id': item.get('id'), 'sha256': item.get('sha256')}
                                     for item in refs], intent)
        if projected != expected:
            raise ValueError('本轮文本材料目录与冻结的版本不同，没有发给模型。')

    def _frozen_file_input_refs(self, intent, chat_id, frozen_inputs):
        refs = self._file_input_refs(intent, chat_id)
        self._validate_text_file_input_refs(intent, refs, frozen_inputs)
        return refs

    @admitted_service
    def file_content(self, chat_id, artifact_id):
        if self.store.changes(chat_id)['selected'] is None:
            raise KeyError('找不到这段对话。')
        try:
            return self.file_tasks.artifact_content(artifact_id, chat_id=chat_id)
        except FileTaskStoreError as error:
            if error.code == 'not_found':
                raise KeyError('这份文件成果不属于当前聊天，或暂时找不到。') from None
            raise

    def _file_receipts(self, chat_id):
        result = {}
        for run in self.store.file_task_runs(chat_id):
            if run['task_intent']['mode'] != 'files':
                continue
            try:
                task = self.file_tasks.get_task(run['id'], chat_id=chat_id)
                state = task.get('file_state', 'warnings')
                if state == 'no_files':
                    state = 'none'
                result[run['message_id']] = {'run_id': run['id'], 'status': task['status'],
                    'artifact_state': state, 'artifacts': task.get('artifacts', []),
                    'warnings': [FILE_WARNINGS.get(code, '有文件未通过保存核对，原记录保留。')
                                 for code in task.get('warnings', [])], 'events': task.get('events', [])}
            except (KeyError, ValueError, OSError):
                result[run['message_id']] = {'run_id': run['id'], 'status': run['status'],
                    'artifact_state': 'warnings', 'artifacts': [],
                    'warnings': ['文件成果尚未核对；没有把回答正文当作保存证明。']}
        return result

    def _start_worker(self, run):
        # Call while holding worker_lock, and only for a newly accepted run.
        cancel = threading.Event()
        with self.admission.lease('service') as parent:
            child = parent.reserve('worker')
            def work():
                with child.handoff():
                    self._work(run, cancel)
            try:
                worker = threading.Thread(target=work, daemon=True)
                self.workers[run['id']] = (worker, cancel)
                worker.start()
            except BaseException:
                # If a thread really started, cancellation of its reservation
                # is forbidden. Retain ownership instead of hiding that race.
                try:
                    child.cancel()
                except ValueError:
                    self._cleanup_blockers.add(run['id'])
                    raise
                self.workers.pop(run['id'], None)
                self.store.update_run(run['id'], status='failed', error='回答未能启动，未自动重发。')
                raise

    @admitted_service
    def submit(self, chat_id, body):
        text, backend, client_id = body.get('text'), body.get('backend'), body.get('client_id')
        if not isinstance(text, str) or len(text) > 30000:
            raise ValueError('请输入消息，单条最多 30,000 个字符。')
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError('发送请求需要有效的消息标识。')
        if not registered_agent(backend) and 'ai_profile_id' not in body:
            raise ValueError('请选择已接入的 AI 连接。')
        with self.worker_lock:
            previous = self.store.find_submission(chat_id, client_id)
            if previous:
                return {'run': previous}
            if self._memory_stop.is_set():
                raise BusyError('本机服务正在关闭，原草稿保留，请重新打开入口后发送。')
            attachments = self._draft_attachments(body)
            if not text.strip() and not attachments:
                raise ValueError('请写消息或加入图片，再发送。')
            route, snapshot = self._profile_request(body)
            model_id = body.get('model_id', '')
            if route:
                backend = route['backend']
            else:
                route = dict(self.model_catalog.resolve(backend, model_id), requested_model_id=model_id)
            self._require_file_route(route, body.get('task_intent'))
            if self.store.active_count() >= 2:
                raise BusyError('已有两段对话正在回答，请稍后发送。')
            task_intent, file_inputs = self._task_request(body, chat_id)
            file_refs = self._frozen_file_input_refs(task_intent, chat_id, file_inputs)
            clean_text = INDEXER.redact(text.strip())
            package, fingerprint = self._recall_memory(snapshot, clean_text)
            run, created = self.store.submit(chat_id, clean_text, backend, client_id,
                                             model=route.get('model'), provider=route.get('provider'),
                                             context_validator=lambda messages, pins: self._validate_context(messages, pins, route,
                                                                                      snapshot, package, fingerprint, task_intent, file_refs),
                                             route=route, attachment_ids=attachments, profile_snapshot=snapshot,
                                             profile_selection_revision=body.get('profile_selection_revision'),
                                             memory_inputs=package, memory_connection_fingerprint=fingerprint,
                                             task_intent=task_intent, file_inputs=file_inputs)
            if created:
                self._start_worker(run)
            return {'run': run}

    def _context_messages(self, chat_id):
        messages = self._clean_context_messages(self.store.get(chat_id)['messages'])
        # The accepted final user turn is the current input; preserve the same
        # image-source accounting used in its pre-send draft preview.
        for message in reversed(messages):
            if message['role'] == 'user':
                message['is_draft'] = True
                break
        return messages

    def _material_metadata(self, messages):
        return self.materials.metadata_for([material_id for message in messages
                                           for material_id in message.get('attachment_ids', [])])

    def _draft_attachments(self, body):
        ids = self.store.validate_attachment_ids(body.get('attachment_ids', []))
        for material_id in ids:
            self.materials.resolve(material_id)
        return ids

    def _require_image_route(self, route, info):
        if info.get('included_attachment_ids') and (not route or route.get('image_input') != 'supported'):
            raise ValueError('所选模型的这条连接暂不支持图片。请选择已支持图片的模型；文字和图片仍保留。')
        if info.get('included_attachment_ids'):
            adapter = self.adapters.get(route.get('backend'))
            runner = getattr(adapter, 'run', None)
            if not self._accepts_keyword(runner, 'images'):
                raise ValueError('这条 Agent 接入器暂不能传图片，文字和图片仍保留。')

    def _prepare_text_file_messages(self, messages, pinned_ids, intent, current_refs):
        return prepare_text_file_messages(messages, pinned_ids, intent, current_refs,
            read_snapshot=self.file_tasks.input_snapshot,
            max_file_bytes=MAX_DOCUMENT_BYTES, max_total_bytes=MAX_TEXT_FILE_PREPARATION_BYTES,
            max_files=MAX_TEXT_FILE_PREPARATION_FILES)

    def _prepare_context(self, messages, pinned_ids=None, inspect=False, *, snapshot=None, memory_inputs=None, task_intent=None, file_input_refs=None):
        messages = self._clean_context_messages(messages)
        block = memory_context_block(memory_inputs, snapshot)
        intent = validate_intent(task_intent)
        messages = self._prepare_text_file_messages(messages, pinned_ids, intent, file_input_refs or [])
        if intent['mode'] == 'files':
            prompt, info = prepare_file_context(messages, pinned_ids or [], inspect=inspect,
                memory_block=block, input_ids=intent['artifact_ids'], input_refs=file_input_refs or [])
            if block:
                info['memory_recall'] = public_recall_info(memory_inputs, snapshot)
            return prompt, info
        # Reserve the bounded memory prefix plus conversation instruction
        # overhead. None keeps the existing context behavior byte-for-byte.
        budget = 120000 - len(block) - (1024 if block else 0)
        prompt, info = build_context(messages, max_chars=budget, pinned_ids=pinned_ids, inspect=inspect,
                                     attachment_metadata=self._material_metadata(messages))
        if block:
            prompt = block + '\n' + prompt
            if len(prompt) > 120000:
                raise ValueError('本轮记忆与共同聊天超出上下文容量，没有截断后发送。')
            info.update(characters=len(prompt), memory_characters=len(block), capacity_characters=120000,
                        memory_recall=public_recall_info(memory_inputs, snapshot))
        if intent['mode'] == 'chat' and intent['artifact_ids']:
            info.update(task_mode='chat', file_input_ids=list(intent['artifact_ids']),
                        file_input_refs=list(file_input_refs or []))
        return prompt, info

    def _validate_context(self, messages, pinned_ids, route=None, snapshot=None, memory_inputs=None, fingerprint=None, task_intent=None, file_input_refs=None):
        # Pure local preparation happens within the store's acceptance
        # transaction, before either messages or a run can be inserted.
        # This check is local-only inside the acceptance transaction. All
        # service/embedding work has already finished, outside SQLite locks.
        self._require_file_route(route, task_intent)
        if fingerprint is not None:
            try:
                changed = self._memory_fingerprint_for_snapshot(snapshot) != fingerprint
            except (MemoryServiceError, ValueError, OSError, KeyError, TypeError):
                raise MemoryRecallFailed() from None
            if changed:
                raise MemoryRecallFailed()
        _, info = self._prepare_context(messages, pinned_ids=pinned_ids, snapshot=snapshot, memory_inputs=memory_inputs, task_intent=task_intent, file_input_refs=file_input_refs)
        self._require_image_route(route, info)
        images = [self.materials.resolve(material_id) for material_id in info.get('included_attachment_ids', [])]
        return {'context_info': info, 'input_images': [{'id': image['id'], 'sha256': image['sha256']} for image in images]}

    @staticmethod
    def _clean_context_messages(messages):
        from history_view import clean_for_context, display_message
        excluded = {'approval_context', 'agent_context', 'agent_message', 'environment', 'interruption'}
        for message in messages:
            if message.get('status') == 'imported':
                source = message.get('provenance') or {}
                inferred = display_message(message['content'], message.get('backend') or source.get('agent') or '',
                                           message['role'], source.get('kind') or 'root')
                message['content'] = clean_for_context(message)
                if message.get('message_kind') in excluded or inferred.get('message_kind') in excluded:
                    message['attachment_ids'], message['native_attachments'] = [], []
        return messages

    def _preview_draft(self, body):
        text = body.get('text')
        if not isinstance(text, str) or len(text) > 30000:
            raise ValueError('先写这一轮的问题，再查看上下文；单条最多 30,000 个字符。')
        attachments = self.store.validate_attachment_ids(body.get('attachment_ids', []))
        if not text.strip() and not attachments:
            raise ValueError('先写问题或加入图片，再查看上下文。')
        return {'id': 'draft', 'role': 'user', 'status': 'completed',
                'content': INDEXER.redact(text.strip()), 'is_draft': True, 'attachment_ids': attachments}

    def _preview_context(self, messages, draft, notice, pin_chat_id=None, selection=None, body=None):
        selection = selection or {'revision': 0, 'pinned_ids': []}
        messages.append(draft)
        profile_snapshot = None
        package = None
        try:
            route = None
            profile_requested = bool(body and {'ai_profile_id', 'ai_profile_revision', 'memory_profile_id'}.intersection(body))
            profile_error = None
            if profile_requested:
                try:
                    route, profile_snapshot = self._profile_request(body)
                    package, _ = self._recall_memory(profile_snapshot, draft['content'])
                except (ValueError, KeyError) as error:
                    profile_error = error
                    notice = str(error.args[0])
            # A failed recall cannot silently preview a none-memory prompt.
            intent, frozen_files = self._task_request(body or {}, pin_chat_id)
            file_refs = self._frozen_file_input_refs(intent, pin_chat_id, frozen_files)
            _, info = self._prepare_context(messages, pinned_ids=selection['pinned_ids'], inspect=True,
                snapshot=profile_snapshot if package is not None else None, memory_inputs=package,
                task_intent=intent, file_input_refs=file_refs)
            if profile_error:
                info.update(can_send=False, policy='memory_recall_failed' if isinstance(profile_error, MemoryRecallFailed)
                            else 'profile_unavailable', send_block_message=notice)
            if not profile_error and validate_intent((body or {}).get('task_intent'))['mode'] == 'files':
                try:
                    if route is None:
                        if profile_requested:
                            raise ValueError(info.get('send_block_message') or 'AI 配置当前不可用，请重新查看。')
                        route = self.model_catalog.resolve((body or {}).get('backend') or self.preferences.read()['default_backend'],
                                                           (body or {}).get('model_id', ''))
                    self._require_file_route(route, (body or {}).get('task_intent'))
                    info['file_output'] = route.get('file_output', 'unknown')
                except (ValueError, KeyError) as error:
                    info.update(can_send=False, policy='file_output_unavailable', send_block_message=str(error.args[0]))
                    notice = str(error.args[0])
            if info.get('included_attachment_ids'):
                body = body or {}
                try:
                    if route is None:
                        if profile_requested:
                            raise ValueError(info.get('send_block_message') or 'AI 配置当前不可用，请重新查看。')
                        route = self.model_catalog.resolve(body.get('backend') or self.preferences.read()['default_backend'], body.get('model_id', ''))
                    self._require_image_route(route, info)
                    for material_id in info['included_attachment_ids']:
                        self.materials.resolve(material_id)
                    info['image_input'] = route.get('image_input', 'unknown')
                except (ValueError, KeyError) as error:
                    info['can_send'] = False
                    notice = str(error.args[0])
                    info['policy'] = 'image_input_unavailable'
                    info['send_block_message'] = notice
        except ContextCapacityError as error:
            info = dict(error.info)
            notice = str(error)
        records = info.pop('records', [])
        return {'context_info': info, 'records': records, 'notice': notice,
                'pin_chat_id': pin_chat_id, 'context_selection': selection,
                'profile_snapshot': profile_snapshot,
                'memory_info': {'recall': public_recall_info(package, profile_snapshot)}
                    if package is not None else None,
                'draft_text': draft['content'], 'draft_attachment_ids': draft.get('attachment_ids', [])}

    @admitted_service
    def new_context_preview(self, body):
        return self._preview_context([], self._preview_draft(body),
            '这是新话题首条消息的上下文检查，按完整草稿准备，包含已放入的引用材料；条目展示开头摘要，可展开草稿全文。尚未创建聊天、写入消息或发给模型。', body=body)

    @admitted_service
    def context_preview(self, chat_id, body):
        draft = self._preview_draft(body)
        result = self.store.get(chat_id)
        return self._preview_context(self._clean_context_messages(result['messages']), draft,
            '这是草稿预览，尚未发给模型。发送时会按最新的共同聊天重新准备；失败或停止的回答不带入。',
            chat_id, result['context_selection'], body=body)

    @admitted_service
    def native_material_preview(self, body):
        fields = {key: value for key, value in body.items() if key != '_one_ai_csrf'}
        if set(fields) != {'material_refs'}:
            raise ValueError('材料预览只接受已保存的材料引用。')
        if self._native_materials is None:
            raise SpecialistHTTPError(409, 'native_chat_materials_disabled',
                '这个入口尚未启用原生材料。', accepted=False)
        try:
            return dict(self._native_materials.preview(fields['material_refs']),
                        workspace_id=self.workspace_id)
        except NativeMaterialError as error:
            raise SpecialistHTTPError(409, error.code, str(error), accepted=False) from None

    @admitted_service
    def native_handoff(self, chat_id, body=None):
        # Independently frozen chat and artifact metadata, guarded by both
        # revisions. Only selected materials cause full file reads.
        snapshot = self.store.handoff_snapshot(chat_id)
        files = self.file_tasks.handoff_artifacts(chat_id)
        current = self.store.changes(chat_id)['selected']
        if (current is None or current['revision'] != snapshot['revision']
                or self.file_tasks.revision(chat_id) != files['revision']):
            raise native_handoff.HandoffConflict('聊天或成果正在变化，请重新打开交接并核对。')
        by_run = {}
        for artifact in files['artifacts']:
            if artifact['chat_id'] != chat_id:
                raise native_handoff.HandoffConflict('成果归属与当前聊天不一致，整份未交接。')
            by_run.setdefault(artifact['run_id'], []).append(artifact)
        for message in snapshot['messages']:
            outputs = by_run.get(message.get('file_task_run_id'), [])
            if outputs:
                message['text_file_outputs'] = [
                    {key: item[key] for key in ('id', 'sha256')} for item in outputs]
                message['text_file_output_refs'] = [
                    {key: item[key] for key in ('id', 'sha256', 'chat_id', 'name')} for item in outputs]
        snapshot['revision'] += ':files:' + files['revision']
        snapshot['messages'] = self._clean_context_messages(snapshot['messages'])
        if body is None:
            return native_handoff.candidates(snapshot, self.workspace_id)
        fields = {key: value for key, value in body.items() if key != '_one_ai_csrf'}
        target = fields.pop('target_agent', None)
        if target not in (None, 'hermes', 'claude'):
            raise ValueError('此交接编码目标尚未接入。')
        encoder = None
        if target == 'hermes':
            from hermes_material_inputs import material_json
            encoder = material_json
        elif target == 'claude':
            encoder = native_handoff.quoted_reference_json
        def encoded_result(result):
            return dict(result, target_agent=target) if target is not None else result
        if fields.get('material_selections'):
            if self._native_materials is None:
                raise SpecialistHTTPError(409, 'native_chat_materials_disabled',
                    '这个入口尚未启用原生材料。', accepted=False)
            plan = native_handoff.material_plan(snapshot, self.workspace_id, fields)
            try:
                refs = [dict(ref, sha256=self._native_materials.metadata_image(ref['id'])['sha256'])
                        if ref['kind'] == 'image' else dict(ref) for ref in plan['material_refs']]
                preview = self._native_materials.preview(refs)
                return encoded_result(native_handoff.with_materials(plan, preview, encoder=encoder,
                                      separate_task=target in ('hermes', 'claude')))
            except (NativeMaterialError, MaterialReferenceError) as error:
                if isinstance(error, MaterialReferenceError):
                    error = NativeMaterialError(error.code)
                raise SpecialistHTTPError(409, error.code, str(error), accepted=False) from None
        if 'material_selections' in fields:
            if type(fields['material_selections']) is not list or fields['material_selections']:
                raise ValueError('所选材料格式无效。')
            fields.pop('material_selections')
        return encoded_result(native_handoff.prepare(snapshot, self.workspace_id, fields, encoder=encoder,
                              separate_task=target in ('hermes', 'claude')))

    @admitted_service
    def history_context_preview(self, body):
        draft = self._preview_draft(body)
        source = body.get('source')
        self._validate_history_source(source)
        reuse, creation_key = self._reuse_source(body), self._creation_key(body)
        if reuse:
            with self.store.lock:
                previous = (self.store.find_creation(creation_key) if creation_key else None) or self.store.find_source(source)
                if previous:
                    result = self.store.get(previous['id'])
                    return self._preview_context(self._clean_context_messages(result['messages']), draft,
                        '这是已接续副本当前前文的草稿预览，包含已保存的后续聊天。尚未创建副本、写入消息或发给模型；这是时刻快照，未锁定发送，提交时会按该副本最新前文重新准备。',
                        previous['id'], result['context_selection'], body=body)
        _, imported = self._read_history_source(source)
        # Use precisely the projection used by ChatStore.create, without any
        # database writes. Source IDs aid inspection; persistent IDs are UUIDs.
        messages = [imported_message(message, 'history:' + source['agent'] + ':' +
                    source['session_id'] + ':' + str(message.get('message_id') or index))
                    for index, message in enumerate(imported)]
        messages = self._clean_context_messages([message for message in messages if message is not None])
        return self._preview_context(messages, draft,
            '这是原历史当前前文的草稿预览，尚未创建接续副本或发给模型；这是时刻快照，未锁定发送。首次发送时会建立或复用共同聊天，按当时最新的前文准备上下文；较长时会省略部分旧记录。要固定必带前文，请先带入共同聊天。', body=body)

    def _work(self, run, cancel):
        run_id = run['id']
        try:
            self._require_file_route(run.get('route'), run.get('task_intent'))
            self._validate_text_file_input_refs(run.get('task_intent'),
                (run.get('context_info') or {}).get('file_input_refs', []), run.get('file_inputs') or [])
            self.store.update_run(run_id, status='running')
            # Read the accepted immutable recall package; never retrieve a
            # second set of facts or substitute today's configuration.
            memory_inputs = self.store.get_memory_inputs(run_id)
            messages = self._context_messages(run['chat_id'])
            prompt, info = self._prepare_context(messages, pinned_ids=run.get('context_pins') or [],
                snapshot=run.get('request_snapshot'), memory_inputs=memory_inputs, task_intent=run.get('task_intent'),
                file_input_refs=(run.get('context_info') or {}).get('file_input_refs', []))
            self._require_image_route(run.get('route'), info)
            images = [self.materials.resolve(material_id) for material_id in info.get('included_attachment_ids', [])]
            frozen = run.get('input_images') or []
            if frozen != [{'id': image['id'], 'sha256': image['sha256']} for image in images]:
                raise ValueError('本轮的图片材料与已接收快照不同，没有发给模型；请检查材料后重新发送。')
            if (run.get('context_info') or {}).get('included_text_file_inputs', []) != info.get('included_text_file_inputs', []):
                raise ValueError('本轮的文本材料与已接收快照不同，没有发给模型；请检查材料后重新发送。')
            self.store.update_run(run_id, context_info=info)
            if cancel.is_set():
                raise InterruptedError('已停止这次回答。')

            def event(value):
                if not cancel.is_set() and value.get('type') == 'text':
                    self.store.update_run(run_id, text=INDEXER.redact(value.get('text') or ''))
                elif run.get('task_intent', {}).get('mode') == 'files' and value.get('type') in ('file_tool', 'tool'):
                    receipt = dict(value)
                    receipt['name'] = value.get('name', value.get('tool'))
                    receipt['state'] = ('failed' if value.get('failed') else value.get('state', value.get('phase')))
                    self.file_tasks.record_event(run_id, receipt, chat_id=run['chat_id'])

            is_file_task = run.get('task_intent', {}).get('mode') == 'files'
            task_cwd = self.workspace
            if is_file_task:
                self._require_file_route(run.get('route'), run.get('task_intent'))
                self.file_tasks.prepare(run_id, run['chat_id'], run['task_intent']['artifact_ids'],
                                        expected_inputs=run.get('file_inputs') or [])
                task = self.file_tasks.start(run_id, chat_id=run['chat_id'])
                if task['status'] == 'cancelled':
                    raise InterruptedError('已停止这次回答。')
                if task['status'] != 'running':
                    raise ValueError('本轮文件任务已有结束回执，没有再次执行。')
                task_cwd = self.file_tasks.task_root(run_id, chat_id=run['chat_id'])
                if cancel.is_set():
                    self.file_tasks.cancel(run_id, chat_id=run['chat_id'])
                    raise InterruptedError('已停止这次回答。')
            runner = (self.file_adapters if is_file_task else self.adapters)[run['backend']].run
            # Older test/integration connectors may not yet accept a route;
            # production adapters take the immutable accepted route explicitly.
            supports_route = self._accepts_keyword(runner, 'route')
            supports_images = self._accepts_keyword(runner, 'images')
            if run.get('request_snapshot') and not supports_route:
                raise ValueError('接入器不能执行本轮冻结的 AI 配置，没有忽略配置后调用。')
            if images and not supports_images:
                raise ValueError('这条 Agent 接入器暂不能传图片，没有把图片省略后发送。')
            options = {'route': run['route']} if supports_route and run.get('route') else {}
            if images:
                options['images'] = images
            result = runner(prompt, str(task_cwd), event, cancel,
                            **options)
            with self.worker_lock:
                if cancel.is_set():
                    self.store.update_run(run_id, status='cancelled')
                else:
                    self.store.update_run(run_id, status='completed', text=INDEXER.redact(result['text']),
                                          native_session_id=result.get('native_session_id'),
                                          model=result.get('model'), provider=result.get('provider'),
                                          model_source=result.get('model_source') if result.get('model_source') in
                                          ('requested_route', 'adapter_reported', 'service_reported') else None)
        except InterruptedError:
            self.store.update_run(run_id, status='cancelled', error='已停止这次回答。')
        except Exception as error:
            with self.worker_lock:
                diagnostic = getattr(error, 'safe_diagnostic', None)
                if isinstance(diagnostic, dict) and diagnostic.get('code') == 'cleanup_unconfirmed':
                    # Cancellation is a request, not proof that an adapter's
                    # child process stopped. Preserve that failure explicitly.
                    self._cleanup_blockers.add(run_id)
                    with self.store.lock, self.store._open() as db:
                        db.execute('INSERT OR IGNORE INTO service_cleanup_blockers VALUES(?,?)',
                                   (run_id, 'cleanup_unconfirmed'))
                    self.store.update_run(run_id, status='failed',
                        error='请求已中止，但后台进程终止尚未确认。请检查连接状态后再发送。')
                elif cancel.is_set():
                    self.store.update_run(run_id, status='cancelled', error='已停止这次回答。')
                else:
                    self.store.update_run(run_id, status='failed', error=INDEXER.redact(str(error))[:1500])
        finally:
            if run.get('task_intent', {}).get('mode') == 'files':
                try:
                    self.file_tasks.finish(run_id, self.store.run(run_id)['status'], chat_id=run['chat_id'])
                except (ValueError, KeyError, OSError):
                    # The answer and the durable file receipt remain separate.
                    # A later startup can reconcile saved bytes without a new
                    # model request. Missing proof never creates a saved card.
                    pass
            self._memory_wake.set()
            with self.worker_lock:
                self.workers.pop(run_id, None)

    @admitted_service
    def cancel(self, run_id):
        run = self.store.run(run_id)
        requested = False
        with self.worker_lock:
            active = self.workers.get(run_id)
            run = self.store.run(run_id)
            if active and run['status'] in ('queued', 'running'):
                active[1].set()
                requested = True
                if run['status'] in ('queued', 'running') and run.get('task_intent', {}).get('mode') == 'files':
                    try:
                        self.file_tasks.cancel(run_id, chat_id=run['chat_id'])
                    except FileTaskStoreError as error:
                        if error.code != 'not_found':
                            raise
                        # Preparation has not yet created its owned root.
                self.store.update_run(run_id, error='正在停止这次回答。')
        return {'ok': True, 'cancel_requested': requested}

    def request_shutdown(self):
        """Signal accepted workers; leave their stores and HTTP leases intact."""
        with self.worker_lock:
            self._memory_stop.set()
            self._memory_wake.set()
            for _worker, cancel in self.workers.values():
                cancel.set()

    def shutdown(self):
        with self.worker_lock:
            self._memory_stop.set()
            self._memory_wake.set()
            source_repair_thread = self._source_repair_thread
            workers = list(self.workers.values())
            for run_id, (worker, cancel) in list(self.workers.items()):
                cancel.set()
                try:
                    run = self.store.run(run_id)
                except KeyError:
                    # An unregistered worker still has to be stopped/joined;
                    # never release the exclusive lock merely for missing DB
                    # metadata. It has no host-owned file task to mark.
                    continue
                if run['status'] in ('queued', 'running') and run.get('task_intent', {}).get('mode') == 'files':
                    try:
                        self.file_tasks.cancel(run_id, chat_id=run['chat_id'])
                    except FileTaskStoreError as error:
                        if error.code != 'not_found':
                            raise
        for worker, cancel in workers:
            worker.join(timeout=5)
        if any(worker.is_alive() for worker, _cancel in workers):
            raise BusyError('回答仍在收尾，服务独占锁暂时保留。')
        if self._cleanup_blockers:
            raise BusyError('后台进程清理尚未证实，服务独占锁暂时保留。')
        if self._memory_thread is not None:
            self._memory_thread.join(timeout=50)
            if self._memory_thread.is_alive():
                # Retain exclusive ownership while an old transport can still
                # finish. Process exit will release it after all threads die.
                raise BusyError('记忆任务仍在收尾，服务独占锁暂时保留。')
        if source_repair_thread is not None:
            source_repair_thread.join(timeout=5)
            if source_repair_thread.is_alive():
                raise BusyError('文件输入仍在收尾，服务独占锁暂时保留。')
        self._close_specialist()
        self._close_native_chats()
        self._close_pi_native_chats()
        self._close_hermes_native_chats()
        self._close_claude_native_chats()
        self._close_dsh_native_chats()
        self._close_interface_tasks()
        if self._external_work is not None:
            self._external_work.close()
        if self._lock_fd is not None:
            if not self.publication_lock.acquire(timeout=5):
                raise BusyError('项目提交仍在收尾，服务独占锁暂时保留。')
            try:
                if self.markdown_chats is not None:
                    self.markdown_chats.close()
                if self.publications is not None:
                    self.publications.close()
            finally:
                self.publication_lock.release()
            self.document_tickets.clear()
            self.file_tasks.close()
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None


class Handler(BaseHTTPRequestHandler):
    server_version = 'OneAI/0.1'

    def setup(self):
        super().setup()
        # Include request headers in the same finite socket I/O budget as the
        # body; a half-open client must not keep HTTP shutdown alive forever.
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # URLs can contain private search text; omit routine request logging.
        pass

    @property
    def service(self):
        return self.server.service

    def _allowed(self, mutate=False, nonce=None):
        port = self.server.server_address[1]
        allowed = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
        if self.headers.get('Host', '').lower() not in allowed:
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin not in {'http://' + host for host in allowed}:
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        # Keep our nonce separate from IM.codes' outer browser-session CSRF.
        if mutate:
            candidate = self.headers.get('X-OneAI-CSRF') or nonce
            if not isinstance(candidate, str) or not secrets.compare_digest(candidate.encode(), self.service.csrf_token.encode()):
                return False
        return True

    def _reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(raw)

    def _require_bodyless_get(self, code, agent):
        lengths = self.headers.get_all('Content-Length') or []
        if self.headers.get_all('Transfer-Encoding') or lengths not in ([], ['0']):
            # Reject framing ambiguity rather than leaving unread bytes for a
            # second request at an exact, read-only native record boundary.
            self.close_connection = True
            raise SpecialistHTTPError(400, code,
                agent + '原生只读记录只接受不带正文的GET请求。')

    def _guard(self, mutate=False, *, known_unsent=False):
        if not self._allowed(mutate):
            value = {'error': '这个请求不来自本机聊天页面。'}
            if known_unsent:
                value.update(code='specialist_gateway_rejected', accepted=False)
            self._reply(403, value)
            return False
        return True

    def _profile_management_error(self, status, value):
        path = urlsplit(self.path).path
        if (path == '/api/management/ai-profile-operations' or path == '/api/management/ai-profiles'
                or path == app_settings.PATH
                or path.startswith('/api/management/ai-profiles/')
                or path.startswith('/api/management/ai-profile-creations/')
                or path == '/api/management/memory-profiles'
                or path == '/api/management/native-chat-configuration'
                or path == '/api/management/native-chat-memory-configuration'
                or path == '/api/management/memory-profile-operations'
                or path.startswith('/api/management/memory-connections/')
                or path.startswith('/api/management/memory-profile-creations/')):
            value = dict(value, workspace_id=self.service.workspace_id)
        self._reply(status, value)

    def _dispatch(self, action, status=200):
        selection_prefix = {
            '/api/management/native-chat-configuration': 'native_chat_selection_',
            '/api/management/native-chat-memory-configuration': 'native_chat_memory_selection_',
        }.get(urlsplit(self.path).path)
        try:
            result = action()
            self._reply(status, result)
        except SpecialistHTTPError as error:
            if selection_prefix:
                payload = error.payload()
                payload['accepted'] = False
                if not error.code.startswith(selection_prefix):
                    payload = {'error': str(error), 'code': selection_prefix + 'invalid', 'accepted': False}
                self._profile_management_error(error.status, payload)
            else:
                self._reply(error.status, error.payload())
        except NativeChatSelectionConflict as error:
            self._reply(409, {'workspace_id': self.service.workspace_id,
                'error': str(error), 'code': 'native_chat_selection_conflict', 'accepted': False,
                'current_configuration': self.service._native_configuration_payload(error.current)})
        except NativeChatSelectionCorrupt:
            self._reply(409, {'workspace_id': self.service.workspace_id,
                'error': '原生下轮配置记录需要核对；没有覆盖或发送。',
                'code': 'native_chat_selection_corrupt', 'accepted': False})
        except NativeMemorySelectionConflict as error:
            self._reply(409, {'workspace_id': self.service.workspace_id,
                'error': str(error), 'code': 'native_chat_memory_selection_conflict', 'accepted': False,
                'current_memory_configuration': self.service._native_memory_configuration_payload(error.current)})
        except NativeMemorySelectionCorrupt:
            self._reply(409, {'workspace_id': self.service.workspace_id,
                'error': '原生下轮记忆选择需要核对；没有覆盖或发送。',
                'code': 'native_chat_memory_selection_corrupt', 'accepted': False})
        except native_handoff.HandoffConflict as error:
            self._reply(409, {'error': str(error), 'code': 'handoff_source_changed'})
        except app_settings.SettingsConflict as error:
            self._reply(409, {'workspace_id': self.service.workspace_id, 'code': 'settings_version_conflict',
                             'error': str(error), 'current_settings': dict(error.current_settings,
                                 workspace_id=self.service.workspace_id)})
        except external_work.ExternalWorkError as error:
            self._reply(error.status, {'workspace_id': self.service.workspace_id,
                'error': str(error), 'code': error.code, 'accepted': False})
        except ProfileConflict as error:
            value = {'error': str(error), 'profile': error.profile}
            if self.command == 'POST' and urlsplit(self.path).path.startswith('/api/profiles/ai/'):
                value['accepted'] = False
            if urlsplit(self.path).path == '/api/management/ai-profile-operations':
                value['code'] = 'profile_version_conflict'
            self._profile_management_error(409, value)
        except ProfileCreationConflict as error:
            value = {'error': str(error), 'code': 'profile_creation_conflict', 'creation_id': error.creation_id}
            if urlsplit(self.path).path.startswith('/api/profiles/ai'):
                value['accepted'] = False
            self._profile_management_error(409, value)
        except ProfileCreationRejected as error:
            self._profile_management_error(error.status, {'error': str(error),
                'code': 'profile_creation_rejected', 'accepted': False})
        except DefaultProfileConflict as error:
            self._profile_management_error(409, {'error': str(error), 'code': 'default_version_conflict',
                                                  'default_state': error.default_state})
        except MemoryProfileConflict as error:
            if (urlsplit(self.path).path == '/api/management/memory-profile-operations'
                    or urlsplit(self.path).path.startswith('/api/management/memory-connections/')):
                self._profile_management_error(409, {'error': str(error), 'code': 'memory_profile_version_conflict',
                    'profile': memory_management.saved_memory_profile(error.profile, self.service.native_memory_bindings)})
            else:
                self._reply(409, {'error': str(error), 'memory_profile': make_memory_snapshot(error.profile)})
        except MemoryProfileCreationConflict as error:
            self._profile_management_error(409, {'error': str(error), 'code': 'memory_profile_creation_conflict',
                                               'creation_id': error.creation_id})
        except MemoryRecallFailed as error:
            self._reply(503, {'error': str(error), 'code': 'memory_recall_failed', 'accepted': False})
        except ProfileSelectionConflict as error:
            self._reply(409, {'error': str(error), 'profile_selection': error.profile_selection})
        except ContextSelectionConflict as error:
            self._reply(409, {'error': str(error), 'context_selection': error.context_selection})
        except ContextCapacityError as error:
            self._reply(400, {'error': str(error),
                'context_info': {key: value for key, value in error.info.items() if key != 'records'}})
        except BusyError as error:
            self._reply(409, {'error': str(error)})
        except RequestDenied as error:
            if selection_prefix:
                self._profile_management_error(403, {'error': str(error),
                    'code': selection_prefix + 'invalid', 'accepted': False})
            else:
                self._profile_management_error(403, {'error': str(error)})
        except KeyError as error:
            self._profile_management_error(404, {'error': str(error.args[0])})
        except DocumentTicketExpired as error:
            self._reply(410, {'error': str(error), 'code': 'document_preview_expired', 'accepted': False})
        except native_task_input.NativeTaskInputError as error:
            result = {'workspace_id': self.service.workspace_id, 'error': str(error),
                'code': 'native_task_' + error.code}
            if error.outcome == 'not_sent':
                result['accepted'] = False
            else:
                result['result_unknown'] = True
            self._reply(400 if error.code == 'invalid_arguments' else
                503 if error.outcome == 'unknown' else 409, result)
        except MarkdownMoveError as error:
            result = {'workspace_id': self.service.workspace_id, 'error': str(error),
                'code': error.code, 'result_unknown': error.result_unknown}
            if not error.result_unknown:
                result['accepted'] = False
            self._reply(503 if error.result_unknown else 400 if error.code == 'invalid_arguments' else 409, result)
        except MarkdownChatSyncError as error:
            rejected = error.code in ('invalid_arguments', 'invalid_document', 'not_found')
            status = 404 if error.code == 'not_found' else 400 if rejected else 409
            result = {'error': str(error), 'code': error.code}
            if rejected:
                result['accepted'] = False
            self._reply(status, result)
        except ProjectPublishStoreError as error:
            rejected = error.code in ('invalid_arguments', 'target_exists', 'preview_expired', 'not_found')
            status = 410 if error.code == 'preview_expired' else 404 if error.code == 'not_found' else 409 if error.code == 'target_exists' else 400 if error.code == 'invalid_arguments' else 503
            result = {'error': str(error), 'code': error.code}
            if rejected:
                result['accepted'] = False
            self._reply(status, result)
        except (ValueError, TypeError) as error:
            if selection_prefix:
                self._profile_management_error(400, {'error': str(error),
                    'code': selection_prefix + 'invalid', 'accepted': False})
            else:
                self._profile_management_error(400, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._profile_management_error(500, {'error': '本机服务未完成请求。请刷新页面或查看服务状态。'})

    @admitted_http
    def do_GET(self):
        if not self._guard():
            return
        route = urlsplit(self.path)
        query = parse_qs(route.query)

        def arg(name, default=''):
            return query.get(name, [default])[0]

        def number(name, default, upper):
            value = int(arg(name, str(default)))
            if not 0 <= value <= upper:
                raise ValueError('分页参数无效。')
            return value

        def respond():
            path = route.path
            if path == '/api/specialist' or path.startswith('/api/specialist/'):
                if path in ('/api/specialist/tools', '/api/specialist/receipt'):
                    self._require_bodyless_get('specialist_invalid_request', 'Codex')
                return self.service.specialist_request('GET', path, query=route.query)
            if path == '/api/native-chats' or path.startswith('/api/native-chats/'):
                return self.service.native_chat_request('GET', path, query=route.query)
            if path == '/api/pi-chats' or path.startswith('/api/pi-chats/'):
                if path.endswith(('/receipt', '/message', '/image')):
                    self._require_bodyless_get('pi_native_receipt_invalid', 'Pi')
                return self.service.pi_native_request('GET', path, query=route.query)
            if path == '/api/hermes-chats' or path.startswith('/api/hermes-chats/'):
                if path.endswith(('/receipt', '/message', '/image')):
                    self._require_bodyless_get('hermes_native_receipt_invalid', 'Hermes')
                return self.service.hermes_native_request('GET', path, query=route.query)
            if path == '/api/claude-chats' or path.startswith('/api/claude-chats/'):
                if path.endswith(('/receipt', '/history', '/message', '/image')):
                    self._require_bodyless_get('claude_native_receipt_invalid', 'Claude')
                return self.service.claude_native_request('GET', path, query=route.query)
            if path == '/api/dsh-chats' or path.startswith('/api/dsh-chats/'):
                if path.endswith('/receipt'):
                    self._require_bodyless_get('dsh_native_receipt_invalid', 'DSH')
                return self.service.dsh_native_request('GET', path, query=route.query)
            if path == '/api/status':
                return self.service.status()
            handoff = re.fullmatch(r'/api/chats/([^/]+)/native-handoff', path)
            if handoff:
                if route.query:
                    raise ValueError('交接候选不接受额外查询参数。')
                return self.service.native_handoff(unquote(handoff[1]))
            if path == '/api/file-materials':
                return {'materials': self.service.file_tasks.list_sources(limit=100)}
            if path == '/api/project-publications/receipt':
                return self.service.project_publication_receipt(arg('client_id'))
            if path.startswith('/api/project-publications/'):
                return self.service.project_publication(unquote(path[len('/api/project-publications/'):]))
            if path == '/api/file-material-receipts':
                return self.service.file_tasks.find_source_receipt(arg('client_id'))
            if path.startswith('/api/file-materials/'):
                material_id = unquote(path[len('/api/file-materials/'):].removesuffix('/content'))
                if path.endswith('/content'):
                    return self.service._file_source(material_id, content=True)
                return {'material': self.service._file_source(material_id)}
            if path.startswith('/api/materials/'):
                suffix = path[len('/api/materials/'):]
                if suffix.endswith('/thumbnail'):
                    return self.service.materials.content(unquote(suffix[:-len('/thumbnail')]), thumbnail=True)
                if suffix.endswith('/content'):
                    return self.service.materials.content(unquote(suffix[:-len('/content')]))
                return {'material': self.service.materials.get(unquote(suffix))}
            if path == '/api/receipts':
                # Keep explicit empty parameters so malformed identities are
                # rejected rather than silently treated as absent.
                receipt_query = parse_qs(route.query, keep_blank_values=True)
                return self.service.receipt(receipt_query.get('kind', [''])[0],
                    receipt_query.get('client_id', [''])[0], receipt_query.get('chat_id', [None])[0])
            if path == '/api/changes':
                chat_id = arg('chat_id')
                if len(chat_id) > 256:
                    raise ValueError('对话标识无效。')
                return self.service.changes(chat_id or None)
            if path == '/api/library':
                folders = parse_qs(route.query, keep_blank_values=True).get('folder')
                if folders is not None and len(folders) != 1:
                    raise ValueError('请选择一个分组。')
                return self.service.list_library(query=arg('query'), agent=arg('agent', 'all'),
                    offset=number('offset', 0, 1000000), limit=number('limit', 50, 100),
                    include_internal=arg('include_internal') == '1', folder=folders[0] if folders is not None else None)
            if path == '/api/markdown-chats':
                self._require_bodyless_get('markdown_invalid', '对话文件夹')
                if route.query:
                    raise ValueError('对话文件夹不接受查询参数。')
                return self.service.list_markdown_chats()
            if path == '/api/markdown-chats/history-reference':
                self._require_bodyless_get('markdown_invalid', '索引引用')
                query = parse_qs(route.query, keep_blank_values=True)
                if set(query) != {'agent', 'session_id'} or any(len(values) != 1 for values in query.values()):
                    raise ValueError('请选择一段原索引。')
                return self.service.history_markdown_reference(query['agent'][0], query['session_id'][0])
            if path == '/api/markdown-chats/updates/receipt':
                self._require_bodyless_get('markdown_invalid', '快照更新回执')
                query = parse_qs(route.query, keep_blank_values=True)
                if set(query) != {'client_id'} or len(query['client_id']) != 1:
                    raise ValueError('更新回执需要一个原请求编号。')
                return self.service.markdown_update_receipt(query['client_id'][0])
            markdown_update = re.fullmatch(r'/api/markdown-chats/updates/([^/]+)', path)
            if markdown_update:
                self._require_bodyless_get('markdown_invalid', '快照更新预览')
                if route.query:
                    raise ValueError('更新预览不接受额外查询参数。')
                return self.service.markdown_update_preview(unquote(markdown_update[1]))
            if path == '/api/markdown-chats/receipt':
                self._require_bodyless_get('markdown_invalid', '整理回执')
                query = parse_qs(route.query, keep_blank_values=True)
                if set(query) != {'client_id'} or len(query['client_id']) != 1:
                    raise ValueError('整理回执需要一个原请求编号。')
                return self.service.markdown_chat_receipt(query['client_id'][0])
            markdown_preview = re.fullmatch(r'/api/markdown-chats/([^/]+)', path)
            if markdown_preview:
                self._require_bodyless_get('markdown_invalid', '整理预览')
                if route.query:
                    raise ValueError('整理预览不接受查询参数。')
                return self.service.markdown_chat_preview(unquote(markdown_preview[1]))
            if path == '/api/preferences':
                return self.service.preferences.read()
            if path == app_settings.PATH:
                if route.query:
                    raise ValueError('设置目录不接受查询参数。')
                return self.service.managed_settings()
            if path == native_work_status.PATH:
                self._require_bodyless_get('work_status_invalid', '工作状态')
                if route.query:
                    raise SpecialistHTTPError(400, 'work_status_invalid', '工作状态不接受查询参数。')
                return self.service.work_status()
            if path == external_work.PATH:
                self._require_bodyless_get('external_work_invalid', '外部工作')
                if route.query:
                    raise external_work.ExternalWorkError('invalid_arguments')
                return self.service.external_work_snapshot()
            if path == '/api/management':
                return self.service.management_inventory(fresh=arg('refresh') == '1')
            if path == '/api/management/ai-profiles':
                return self.service.saved_ai_profile_inventory()
            if path == '/api/management/conversations':
                self._require_bodyless_get('conversation_query_invalid', '对话目录')
                fields = parse_qs(route.query, keep_blank_values=True, strict_parsing=True, errors='strict')
                if (set(fields) - {'query', 'agent', 'offset', 'limit'}
                        or any(len(values) != 1 for values in fields.values())):
                    raise ValueError('对话目录只接受明确的查询、Agent和分页参数。')
                values = {key: items[0] for key, items in fields.items()}
                for key in ('offset', 'limit'):
                    if key in values:
                        if re.fullmatch(r'0|[1-9][0-9]{0,6}', values[key]) is None:
                            raise ValueError('对话目录分页参数无效。')
                        values[key] = int(values[key])
                return self.service.conversations_list(**values)
            if path == '/api/management/native-tasks':
                self._require_bodyless_get('native_task_result_invalid', '原生任务')
                fields = parse_qs(route.query, keep_blank_values=True, strict_parsing=True, errors='strict')
                if (set(fields) != {'agent', 'chat_id', 'client_id'}
                        or any(len(values) != 1 for values in fields.values())):
                    raise ValueError('请指定一段原生对话和本次原请求编号。')
                return self.service.read_native_task(**{key: values[0] for key, values in fields.items()})
            if path == '/api/management/native-chat-configuration':
                self._require_bodyless_get('native_chat_selection_invalid', '原生下轮配置')
                fields = parse_qs(route.query, keep_blank_values=True, strict_parsing=True)
                if set(fields) != {'agent', 'chat_id'} or any(len(values) != 1 for values in fields.values()):
                    raise ValueError('请只指定一个Agent和一个准确的对话编号。')
                return self.service.native_chat_configuration(fields['agent'][0], fields['chat_id'][0])
            if path == '/api/management/native-chat-memory-configuration':
                self._require_bodyless_get('native_chat_memory_selection_invalid', '原生下轮共享记忆')
                fields = parse_qs(route.query, keep_blank_values=True, strict_parsing=True)
                if set(fields) != {'agent', 'chat_id'} or any(len(values) != 1 for values in fields.values()):
                    raise ValueError('请只指定一个Agent和一个准确的对话编号。')
                return self.service.native_chat_memory_configuration(fields['agent'][0], fields['chat_id'][0])
            if path == '/api/management/memory-profiles':
                self._require_bodyless_get('memory_profile_invalid', '记忆配置目录')
                if route.query:
                    raise ValueError('记忆配置目录不接受查询参数。')
                return self.service.saved_memory_profile_inventory()
            if path.startswith('/api/management/memory-connections/'):
                self._require_bodyless_get('memory_connection_invalid', '记忆连接检查')
                fields = memory_connection_check.query_fields(route.query)
                return self.service.check_memory_connection(
                    unquote(path[len('/api/management/memory-connections/'):]), fields['revision'])
            if path.startswith('/api/management/memory-profile-creations/'):
                self._require_bodyless_get('memory_profile_invalid', '记忆配置回执')
                if route.query:
                    raise ValueError('创建回执只接受固定请求编号。')
                return self.service.memory_profile_creation_receipt(unquote(path[len('/api/management/memory-profile-creations/'):]))
            if path == native_chat_management.PATH:
                self._require_bodyless_get('native_chat_name_invalid', '对话名称目录')
                if route.query:
                    native_chat_management.invalid(self.service)
                return self.service.native_chat_names()
            if path == native_model_management.CONNECTIONS_PATH:
                self._require_bodyless_get('native_model_catalog_invalid', '模型目录')
                if route.query:
                    native_model_management.invalid()
                return self.service.native_model_connections()
            if path == native_model_management.CATALOG_PATH:
                self._require_bodyless_get('native_model_catalog_invalid', '模型目录')
                return self.service.refresh_native_model_catalog(native_model_management.query_fields(route.query))
            if path == native_model_management.SPECIALIST_CATALOG_PATH:
                self._require_bodyless_get('native_model_catalog_invalid', '模型目录')
                if route.query:
                    native_model_management.invalid()
                return self.service.refresh_specialist_model_catalog()
            if path.startswith('/api/management/ai-profile-creations/'):
                if route.query:
                    raise ValueError('创建回执只接受固定请求编号。')
                return self.service.ai_profile_creation_receipt(unquote(path[len('/api/management/ai-profile-creations/'):]))
            if path == '/api/management/context':
                return {'workspace_id': self.service.workspace_id, 'csrf_token': self.service.csrf_token}
            if path == '/api/management/memory-documents':
                if len(route.query) > 256 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                    raise ValueError('记忆文档读取不接收请求正文。')
                document_query = parse_qs(route.query, keep_blank_values=True, strict_parsing=True,
                                          errors='strict', max_num_fields=2)
                if set(document_query) != {'profile_id', 'name'} or any(len(values) != 1 for values in document_query.values()):
                    raise ValueError('记忆文档读取需要准确的配置编号和文档名。')
                return self.service.memory_document({key: values[0] for key, values in document_query.items()})
            if path == '/api/profiles':
                return self.service.profile_inventory()
            if path == '/api/chats':
                return self.service.store.list()
            if path.startswith('/api/chats/'):
                file_match = re.fullmatch(r'/api/chats/([^/]+)/files/([^/]+)', path)
                if file_match:
                    return self.service.file_content(unquote(file_match[1]), unquote(file_match[2]))
                if path.endswith('/profile-selection'):
                    if route.query not in ('', 'metadata_only=1'):
                        raise ValueError('配置选择只接受 metadata_only=1。')
                    return self.service.profile_selection(unquote(path[len('/api/chats/'):-len('/profile-selection')]),
                        metadata_only=bool(route.query))
                if path.endswith('/source-images/receipt'):
                    return self.service.source_images_receipt(
                        unquote(path[len('/api/chats/'):-len('/source-images/receipt')]),
                        arg('plan_version'), arg('chat_revision'))
                if path.endswith('/source-images'):
                    return self.service.source_images(unquote(path[len('/api/chats/'):-len('/source-images')]))
                if path.endswith('/source-update'):
                    return self.service.source_update(unquote(path[len('/api/chats/'):-len('/source-update')]))
                if path.endswith('/context-pins'):
                    return self.service.context_pins(unquote(path[len('/api/chats/'):-len('/context-pins')]))
                return self.service.get_chat(unquote(path.removeprefix('/api/chats/')))
            if path == '/api/history/sessions':
                return self.service.history.list_sessions(agent=arg('agent', 'all'), q=arg('q'),
                    offset=number('offset', 0, 1000000), limit=number('limit', 50, 100),
                    include_internal=arg('include_internal') == '1')
            if path == '/api/history/session':
                return self.service.history.get_session(arg('agent'), arg('session_id'),
                    offset=number('offset', 0, 1000000), limit=number('limit', 100, 100))
            if path == '/api/history/search':
                return self.service.history.search(arg('q'), agent=arg('agent', 'all'),
                    offset=number('offset', 0, 1000000), limit=number('limit', 30, 100),
                    include_internal=arg('include_internal') == '1')
            raise KeyError('找不到这个接口。')

        if route.path.startswith('/api/'):
            self._dispatch(respond)
            return
        try:
            resource = read_resource(route.path, agent_bootstrap=browser_definitions())
        except ResourceNotFound:
            self._reply(404, {'error': '找不到这个页面。'})
            return
        except (OSError, ValueError, UnicodeError):
            self._reply(500, {'error': '界面素材暂时无法读取，请刷新页面或更新小白兔。'})
            return
        raw, csp_nonce = resource.data, resource.nonce
        self.send_response(200)
        self.send_header('Content-Type', resource.content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        nonce_policy = " 'nonce-" + csp_nonce + "'" if csp_nonce else ''
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'" + nonce_policy +
                         "; style-src 'self'" + nonce_policy + "; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    @admitted_http
    def do_POST(self):
        raw_path = self.path.partition('?')[0]
        specialist_mutation = (raw_path == '/api/specialist' or raw_path.startswith('/api/specialist/')
                               or raw_path == '/api/native-chats' or raw_path.startswith('/api/native-chats/')
                               or raw_path == '/api/pi-chats' or raw_path.startswith('/api/pi-chats/')
                               or raw_path == '/api/hermes-chats' or raw_path.startswith('/api/hermes-chats/')
                               or raw_path == '/api/claude-chats' or raw_path.startswith('/api/claude-chats/')
                               or raw_path == '/api/dsh-chats' or raw_path.startswith('/api/dsh-chats/')
                               or raw_path == '/api/native-materials/preview')
        specialist_mutation = specialist_mutation or raw_path.startswith('/api/native-history/')
        specialist_mutation = specialist_mutation or raw_path == '/api/management/native-chat-configuration'
        specialist_mutation = specialist_mutation or raw_path == '/api/management/native-chat-memory-configuration'
        specialist_mutation = specialist_mutation or raw_path == '/api/markdown-chats/move'
        specialist_mutation = specialist_mutation or raw_path == '/api/management/native-tasks'
        if not self._guard(known_unsent=specialist_mutation):
            return

        def respond():
            route = urlsplit(self.path)
            path = route.path
            native_chat = path == '/api/native-chats' or path.startswith('/api/native-chats/')
            pi_chat = path == '/api/pi-chats' or path.startswith('/api/pi-chats/')
            hermes_chat = path == '/api/hermes-chats' or path.startswith('/api/hermes-chats/')
            claude_chat = path == '/api/claude-chats' or path.startswith('/api/claude-chats/')
            dsh_chat = path == '/api/dsh-chats' or path.startswith('/api/dsh-chats/')
            native_material = path == '/api/native-materials/preview'
            markdown_material = path in ('/api/markdown-chats/file-preview', '/api/file-materials/markdown')
            markdown_move = path == '/api/markdown-chats/move'
            native_task = path == '/api/management/native-tasks'
            external = path == external_work.PATH
            native_history = path.startswith('/api/native-history/')
            native_management = path in ('/api/management/native-chat-configuration',
                                         '/api/management/native-chat-memory-configuration',
                                         native_model_management.PREPARATION_PATH)
            specialist = native_history or dsh_chat or claude_chat or hermes_chat or pi_chat or native_chat or path == '/api/specialist' or path.startswith('/api/specialist/')
            # This boundary finishes all local body/CSRF checks before any
            # holder method. Its rejection applies to this attempt only.
            try:
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('请使用 JSON 消息。')
                raw = self._read_body(external_work.MAX_BODY if external else 256 * 1024 if native_task else 8192 if native_management or markdown_move else app_settings.MAX_BODY if path == app_settings.PATH else
                                      32 * 1024 if path in ('/api/management/memory-profile-operations', '/api/management/memory-documents') else MAX_MATERIAL_BODY if path == '/api/materials' else
                                      MAX_TEXT_MATERIAL_BODY if path == '/api/file-materials' else MAX_BODY)
                if path in ('/api/management/memory-profile-operations', '/api/management/memory-documents'):
                    try:
                        body = decode_specialist_body(raw)
                    except SpecialistHTTPError:
                        raise ValueError('记忆配置操作需要不含重复字段的有效 JSON 对象。') from None
                else:
                    if markdown_material or markdown_move:
                        try:
                            body = decode_specialist_body(raw)
                        except SpecialistHTTPError:
                            raise ValueError('文件资料需要不含重复字段的有效 JSON 对象。') from None
                    else:
                        body = (external_work.decode_body(raw) if external else app_settings.decode_body(raw) if path == app_settings.PATH else
                                decode_specialist_body(raw) if specialist or native_material or native_management or native_task else json.loads(raw))
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效。')
                if not self._allowed(mutate=True, nonce=body.get('_one_ai_csrf')):
                    raise RequestDenied('消息校验未通过。请重新打开聊天页面。')
            except SpecialistHTTPError as error:
                error.accepted = False
                raise
            except RequestDenied:
                if not specialist and not native_material:
                    raise
                raise SpecialistHTTPError(403, 'specialist_gateway_rejected',
                    '消息校验未通过，这次请求未发送，请重新打开聊天页面。', accepted=False) from None
            except (ValueError, TypeError):
                if not specialist and not native_material:
                    raise
                raise SpecialistHTTPError(400, 'specialist_gateway_invalid',
                    '专员请求格式或大小无效，这次请求未发送，请修改后再试。', accepted=False) from None
            if native_history:
                parts = path.removeprefix('/api/native-history/').split('/')
                if (self.command != 'POST' or route.query or len(parts) != 2
                        or parts[0] not in ('codex', 'hermes', 'pi', 'claude', 'dsh') or parts[1] not in ('preview', 'adopt')):
                    raise SpecialistHTTPError(400, 'native_history_invalid', '请使用原会话核对或登记入口。', accepted=False)
                return self.service.native_history_request(parts[0], parts[1], body)
            if external:
                if self.command != 'POST' or route.query:
                    raise external_work.ExternalWorkError('invalid_arguments')
                return self.service.external_work_operation({key: value for key, value in body.items()
                    if key != '_one_ai_csrf'})
            if native_material:
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用材料预览的固定 POST 入口。')
                return self.service.native_material_preview(body)
            if path in ('/api/markdown-chats/folders', '/api/markdown-chats/open-folder'):
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用分组的固定入口。')
                return (self.service.create_markdown_folder(body) if path.endswith('/folders')
                        else self.service.open_markdown_folder(body))
            if path == '/api/management/memory-documents':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用记忆文档的固定保存入口。')
                return self.service.memory_document(body, writing=True)
            if markdown_move:
                if self.command != 'POST' or route.query:
                    raise MarkdownMoveError('invalid_arguments')
                return self.service.move_markdown_file(body)
            if native_task:
                if self.command != 'POST' or route.query:
                    raise native_task_input.NativeTaskInputError('invalid_arguments', 'not_sent')
                return self.service.dispatch_native_task(body)
            if specialist:
                if dsh_chat:
                    return self.service.dsh_native_request(self.command, path, query=route.query, body=body)
                if claude_chat:
                    return self.service.claude_native_request(self.command, path, query=route.query, body=body)
                if hermes_chat:
                    return self.service.hermes_native_request(self.command, path, query=route.query, body=body)
                if pi_chat:
                    return self.service.pi_native_request(self.command, path, query=route.query, body=body)
                if native_chat:
                    return self.service.native_chat_request(self.command, path, query=route.query, body=body)
                return self.service.specialist_request(self.command, path, query=route.query, body=body)
            if path == app_settings.PATH:
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用设置管理的固定POST入口。')
                return self.service.update_managed_settings({key: value for key, value in body.items()
                                                            if key != '_one_ai_csrf'})
            if path == '/api/management/ai-profile-operations':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用AI配置管理的固定操作入口。')
                return self.service.apply_ai_profile_operation(body)
            if path == '/api/management/native-chat-configuration':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用原生下轮配置的固定POST入口。')
                return self.service.select_native_chat_configuration(body)
            if path == '/api/management/native-chat-memory-configuration':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用原生下轮共享记忆的固定POST入口。')
                return self.service.select_native_chat_memory_configuration(body)
            if path == native_model_management.PREPARATION_PATH:
                if self.command != 'POST' or route.query:
                    native_model_management.invalid()
                return self.service.prepare_native_model_catalog(body)
            if path == '/api/management/memory-profile-operations':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用记忆配置管理的固定操作入口。')
                return self.service.apply_memory_profile_operation(body)
            if path == native_chat_management.PATH:
                if self.command != 'POST' or route.query:
                    native_chat_management.invalid(self.service)
                return self.service.rename_native_chat(body)
            if path in ('/api/file-materials', '/api/file-materials/project', '/api/file-materials/markdown'):
                if self.command != 'POST' or path.endswith('/markdown') and route.query:
                    raise ValueError('请使用添加文本文件入口。')
                return self.service.import_file_material(body,
                    project_preview=path.endswith(('/project', '/markdown')),
                    markdown_preview=path.endswith('/markdown'))
            if path == '/api/file-materials/artifact':
                if self.command != 'POST':
                    raise ValueError('请使用成果导入入口。')
                return self.service.import_artifact_material(body)
            if path == '/api/markdown-chats/file-preview':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用Markdown文件资料预览入口。')
                return self.service.markdown_file_preview(body)
            markdown_update = re.fullmatch(r'/api/markdown-chats/updates/([^/]+)/confirm', path)
            if path == '/api/markdown-chats/updates/preview' or markdown_update:
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用快照更新的固定 POST 入口。')
                if markdown_update:
                    return self.service.confirm_markdown_update(unquote(markdown_update[1]), body)
                return self.service.prepare_markdown_update(body)
            if path == '/api/markdown-chats/native-preview':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用原生整理的固定 POST 入口。')
                return self.service.prepare_native_markdown_chat(body)
            if path == '/api/markdown-chats/history-reference-preview':
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用索引整理的固定 POST 入口。')
                return self.service.prepare_history_markdown_reference(body)
            markdown_preview = re.fullmatch(r'/api/chats/([^/]+)/markdown-preview', path)
            markdown_confirm = re.fullmatch(r'/api/markdown-chats/([^/]+)/confirm', path)
            if markdown_preview or markdown_confirm:
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用整理文件夹的固定 POST 入口。')
                if markdown_preview:
                    return self.service.prepare_markdown_chat(unquote(markdown_preview[1]), body)
                return self.service.confirm_markdown_chat(unquote(markdown_confirm[1]), body)
            publication_preview = re.fullmatch(r'/api/chats/([^/]+)/files/([^/]+)/project-preview', path)
            if publication_preview:
                if self.command != 'POST':
                    raise ValueError('请使用原项目另存入口。')
                return self.service.prepare_project_publication(unquote(publication_preview[1]), unquote(publication_preview[2]), body)
            publication_confirm = re.fullmatch(r'/api/project-publications/([^/]+)/confirm', path)
            if publication_confirm:
                if self.command != 'POST':
                    raise ValueError('请使用原项目提交入口。')
                return self.service.confirm_project_publication(unquote(publication_confirm[1]), body)
            if path == '/api/materials':
                if self.command != 'POST':
                    raise ValueError('请用添加图片入口上传。')
                return {'material': self.service.materials.upload(body.get('name'), body.get('data_base64'))}
            if self.command == 'PUT':
                management_name = re.fullmatch(r'/api/management/ai-profiles/([^/]+)/name', path)
                if management_name:
                    return self.service.rename_ai_profile(unquote(management_name[1]), body)
                if path.startswith('/api/profiles/memory/'):
                    return self.service.update_memory_profile(unquote(path[len('/api/profiles/memory/'):]), body)
                if path.startswith('/api/profiles/ai/'):
                    return self.service.update_ai_profile(unquote(path[len('/api/profiles/ai/'):]), body)
                if path.startswith('/api/chats/') and path.endswith('/profile-selection'):
                    if route.query not in ('', 'metadata_only=1'):
                        raise ValueError('配置选择只接受 metadata_only=1。')
                    return self.service.set_profile_selection(unquote(path[len('/api/chats/'):-len('/profile-selection')]), body,
                        metadata_only=bool(route.query))
                if path.startswith('/api/chats/') and path.endswith('/backend'):
                    chat_id = unquote(path[len('/api/chats/'):-len('/backend')])
                    return self.service.set_chat_backend(dict(body, chat_id=chat_id))
                raise KeyError('找不到这个接口。')
            if path == '/api/chats':
                return self.service.create(body)
            if path == '/api/profiles/ai':
                return self.service.create_ai_profile(body)
            if path == '/api/profiles/memory':
                return self.service.create_memory_profile(body)
            if path == '/api/profiles/memory/native-hermes':
                return self.service.attach_native_memory(body)
            if path.startswith('/api/profiles/memory/') and path.endswith('/copy'):
                return self.service.update_memory_profile(unquote(path[len('/api/profiles/memory/'):-len('/copy')]), body, copying=True)
            if path == '/api/profiles/default':
                return self.service.set_default_profile(body)
            if path.startswith('/api/profiles/ai/') and path.endswith('/copy'):
                return self.service.update_ai_profile(unquote(path[len('/api/profiles/ai/'):-len('/copy')]), body, copying=True)
            if path == '/api/preferences':
                return self.service.update_preferences(body.get('patch'))
            if path == '/api/preferences/chat':
                return self.service.set_chat_backend(body)
            if path == '/api/history/refresh':
                return self.service.refresh()
            if path == '/api/history/context-preview':
                return self.service.history_context_preview(body)
            if path == '/api/context-preview':
                return self.service.new_context_preview(body)
            if path == '/api/local-document':
                return self.service.local_document(body)
            handoff = re.fullmatch(r'/api/chats/([^/]+)/native-handoff', path)
            if handoff:
                if self.command != 'POST' or route.query:
                    raise ValueError('请使用交接预览入口。')
                return self.service.native_handoff(unquote(handoff[1]), body)
            if path.startswith('/api/chats/') and path.endswith('/source-update'):
                chat_id = unquote(path[len('/api/chats/'):-len('/source-update')])
                return self.service.append_source_update(chat_id, body)
            if path.startswith('/api/chats/') and path.endswith('/source-images'):
                chat_id = unquote(path[len('/api/chats/'):-len('/source-images')])
                return self.service.backfill_source_images(chat_id, body)
            if path.startswith('/api/chats/') and path.endswith('/context-preview'):
                chat_id = unquote(path[len('/api/chats/'):-len('/context-preview')])
                return self.service.context_preview(chat_id, body)
            if path.startswith('/api/chats/') and path.endswith('/context-pins'):
                chat_id = unquote(path[len('/api/chats/'):-len('/context-pins')])
                return self.service.set_context_pin(chat_id, body)
            if path.startswith('/api/chats/') and path.endswith('/messages'):
                chat_id = unquote(path[len('/api/chats/'):-len('/messages')])
                return self.service.submit(chat_id, body)
            if path.startswith('/api/runs/') and path.endswith('/cancel'):
                return self.service.cancel(unquote(path[len('/api/runs/'):-len('/cancel')]))
            raise KeyError('找不到这个接口。')

        path = urlsplit(self.path).path
        sending = path.startswith('/api/chats/') and path.endswith('/messages')
        status = 202 if sending else 200
        self._dispatch(respond, status=status)

    def do_PUT(self):
        self.do_POST()

    def _read_body(self, max_bytes=MAX_BODY):
        """Bounded HTTP/1.1 bodies, including the daemon's chunked upload."""
        self.connection.settimeout(20)
        transfer = self.headers.get('Transfer-Encoding')
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) > 1 or (transfer and lengths):
            raise ValueError('请求正文的长度标记冲突。')
        if not transfer:
            size = int(lengths[0]) if lengths else 0
            if not 0 < size <= max_bytes:
                raise ValueError('请求为空或太大。')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('请求正文没有完整收到。')
            return raw
        if transfer.strip().lower() != 'chunked':
            raise ValueError('不支持这种请求正文编码。')
        parts, total = [], 0
        for _ in range(4096):
            line = self.rfile.readline(130)
            if not line.endswith(b'\r\n') or len(line) > 128:
                raise ValueError('分块正文格式无效。')
            size_text = line[:-2].split(b';', 1)[0]
            if not re.fullmatch(rb'[0-9a-fA-F]{1,16}', size_text):
                raise ValueError('分块正文的长度无效。')
            size = int(size_text, 16)
            if size == 0:
                # Trailer fields cannot replace the authentication or nonce.
                for _ in range(32):
                    trailer = self.rfile.readline(1026)
                    if trailer == b'\r\n':
                        if not total:
                            raise ValueError('请求正文为空。')
                        return b''.join(parts)
                    if not trailer.endswith(b'\r\n') or len(trailer) > 1024:
                        raise ValueError('分块正文的尾部无效。')
                raise ValueError('分块正文的尾部太长。')
            total += size
            if total > max_bytes:
                raise ValueError('请求正文太大。')
            part = self.rfile.read(size)
            if len(part) != size or self.rfile.read(2) != b'\r\n':
                raise ValueError('分块正文没有完整收到。')
            parts.append(part)
        raise ValueError('请求正文分块数量太多。')

    @admitted_http
    def do_OPTIONS(self):
        self._reply(403, {'error': '仅允许本机页面访问。'})


def main():
    parser = argparse.ArgumentParser(description='一站式 AI 本机聊天原型')
    parser.add_argument('--port', type=int, default=17860)
    parser.add_argument('--data-dir', default=str(Path.home() / '.imcodes' / 'one-ai'))
    parser.add_argument('--index', help='独立历史索引，只能是数据目录内的history.sqlite')
    parser.add_argument('--documents-dir', help='仅允许预览这个项目目录内的 .md/.txt 文件；默认禁用')
    parser.add_argument('--publication-dir', help='明确允许把生成成果另存为此项目的新.md/.txt；不覆盖既有目标，默认禁用')
    parser.add_argument('--specialist-mcp-python',
                        help='开发验收：以现有MCP虚拟环境的Python绝对路径显式连接原生Codex专员；默认关闭')
    parser.add_argument('--specialist-codex-cli',
                        help='仅与专员入口一起使用：固定现有Codex可执行文件绝对路径')
    parser.add_argument('--specialist-work-document', action='store_true',
                        help='仅与专员入口一起使用：从项目目录准备专员独立工作说明；默认关闭')
    parser.add_argument('--native-codex-cli',
                        help='开发验收：现有Codex可执行文件绝对路径，仅连接原生会话，不添加应用管理工具；默认关闭')
    parser.add_argument('--native-chat-mcp-python',
                        help='开发验收：普通原生Codex附加小白兔管理MCP，指定现有MCP Python；与native-codex-cli互斥')
    parser.add_argument('--native-pi-node', help='开发验收：Pi原生RPC使用的Node绝对路径；默认关闭')
    parser.add_argument('--native-pi-cli', help='开发验收：已安装Pi的cli.js绝对路径；与native-pi-node同时提供')
    parser.add_argument('--native-hermes-node', help='开发验收：Hermes官方通信客户端使用的Node绝对路径；默认关闭')
    parser.add_argument('--native-hermes-python', help='开发验收：现有Hermes虚拟环境Python绝对路径')
    parser.add_argument('--native-hermes-root', help='开发验收：现有Hermes源码目录；与前两个参数同时提供')
    parser.add_argument('--native-hermes-home', help='开发验收：可选固定Hermes资料目录，不改变模型或权限规则')
    parser.add_argument('--native-hermes-session-settings', action='store_true',
                        help='开发验收：启用版本限定的原生会话设置校验层；默认关闭')
    parser.add_argument('--native-claude-python', help='开发验收：已有官方Claude SDK的Python绝对路径；默认关闭')
    parser.add_argument('--native-claude-cli', help='开发验收：现有用户Claude CLI绝对路径，与Python成对提供')
    parser.add_argument('--native-dsh-node', help='开发验收：DSH原生完整profile使用的Node绝对路径；默认关闭')
    parser.add_argument('--native-dsh-cli', help='开发验收：已安装DSH原生lib/bin.js绝对路径')
    parser.add_argument('--native-dsh-root', help='开发验收：DSH原生包目录；与前两个参数同时提供')
    parser.add_argument('--native-dsh-home', help='开发验收：可选固定DSH资料目录；不改变模型或工具规则')
    parser.add_argument('--native-dsh-history-root',
                        help='可选固定DSH会话存储根目录，用于只读历史索引；默认资料目录下的sessions')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口需要在 1024–65535 之间。')
    if args.native_codex_cli is not None and args.native_chat_mcp_python is not None:
        parser.error('普通Codex请选纯原生连接或管理扩展连接，不能同时指定两种启动方式。')
    if args.native_codex_cli is not None and not Path(args.native_codex_cli).is_absolute():
        parser.error('请提供现有Codex可执行文件的绝对路径。')
    specialist_connection = None
    native_chat_connection = None
    pi_native_connection = None
    hermes_native_connection = None
    claude_native_connection = None
    dsh_native_connection = None
    dsh_args = (args.native_dsh_node, args.native_dsh_cli, args.native_dsh_root)
    if any(value is not None for value in dsh_args) and not all(value is not None for value in dsh_args):
        parser.error('DSH原生入口需要同时指定Node、原生CLI及其包目录。')
    if args.native_dsh_home is not None and not all(value is not None for value in dsh_args):
        parser.error('DSH资料目录只能与明确配置的原生入口一起使用。')
    if args.native_dsh_history_root is not None and not all(value is not None for value in dsh_args):
        parser.error('DSH历史目录需要已配置的DSH只读解码依赖。')
    if all(value is not None for value in dsh_args):
        from rabbit_runtime.dsh.connection import DshNativeConnection
        try:
            dsh_native_connection = DshNativeConnection(*dsh_args, home=args.native_dsh_home)
        except ValueError as error:
            parser.error(str(error))
    if (args.native_claude_python is None) != (args.native_claude_cli is None):
        parser.error('Claude原生入口需要同时指定已有SDK Python和用户CLI。')
    if args.native_claude_python is not None:
        from rabbit_runtime.claude.connection import ClaudeNativeConnection
        try:
            claude_native_connection = ClaudeNativeConnection(args.native_claude_python, args.native_claude_cli)
        except ValueError as error:
            parser.error(str(error))
    hermes_args = (args.native_hermes_node, args.native_hermes_python, args.native_hermes_root)
    if any(value is not None for value in hermes_args) and not all(value is not None for value in hermes_args):
        parser.error('Hermes原生入口需要同时指定Node、Hermes Python和源码目录。')
    if (args.native_hermes_home is not None or args.native_hermes_session_settings) and not all(value is not None for value in hermes_args):
        parser.error('Hermes资料目录只能与明确配置的原生入口一起使用。')
    if all(value is not None for value in hermes_args):
        from rabbit_runtime.hermes.connection import HermesNativeConnection
        try:
            hermes_native_connection = HermesNativeConnection(*hermes_args, home=args.native_hermes_home,
                session_settings_guard=args.native_hermes_session_settings)
        except ValueError as error:
            parser.error(str(error))
    if (args.native_pi_node is None) != (args.native_pi_cli is None):
        parser.error('Pi原生入口需要同时指定现有Node和Pi CLI。')
    if args.native_pi_node is not None:
        from rabbit_runtime.pi.connection import PiNativeConnection
        try:
            pi_native_connection = PiNativeConnection(args.native_pi_node, args.native_pi_cli, enable_task_results=True)
        except ValueError as error:
            parser.error(str(error))
    if (args.specialist_codex_cli is not None or args.specialist_work_document) and args.specialist_mcp_python is None:
        parser.error('专员CLI与工作说明需要同时指定专员MCP Python入口。')
    if args.specialist_codex_cli is not None and not Path(args.specialist_codex_cli).is_absolute():
        parser.error('专员CLI需要现有可执行文件的绝对路径。')
    if args.specialist_mcp_python is not None:
        from rabbit_specialist.specialist_connection import CodexSpecialistConnection
        try:
            options = {'codex_command': args.specialist_codex_cli} if args.specialist_codex_cli is not None else {}
            specialist_connection = CodexSpecialistConnection(port=args.port, mcp_python=args.specialist_mcp_python,
                                                             tool_script=Path(__file__).absolute().parent.parent / '工具/rabbit_management_mcp.py',
                                                             **options)
        except ValueError as error:
            parser.error(str(error))
    if args.native_chat_mcp_python is not None:
        from rabbit_specialist.specialist_connection import CodexSpecialistConnection
        try:
            native_chat_connection = CodexSpecialistConnection(port=args.port, mcp_python=args.native_chat_mcp_python, tool_script=Path(__file__).absolute().parent.parent / '工具/rabbit_management_mcp.py')
        except ValueError as error:
            parser.error(str(error))
    if args.native_codex_cli is not None:
        from rabbit_runtime.codex_connection import CodexNativeConnection
        try:
            native_chat_connection = CodexNativeConnection(codex_command=args.native_codex_cli)
        except ValueError as error:
            parser.error(str(error))
    os.umask(0o077)
    index = args.index or str(Path(args.data_dir) / 'history.sqlite')
    specialist_options = {'specialist_work_document': True} if args.specialist_work_document else {}
    from memory_program_setup import configured_programs
    # Keep the configured venv path lexical: resolving its Python symlink
    # would select the base interpreter's unrelated site-packages directory.
    hermes_dependencies = (Path(args.native_hermes_python).parent.parent / 'lib' /
        f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
        if args.native_hermes_python else None)
    memory_program_drivers = configured_programs(args.data_dir, hermes_source=args.native_hermes_root,
        hermes_dependencies=hermes_dependencies)
    service = Service(args.data_dir, index, documents_dir=args.documents_dir, publication_dir=args.publication_dir,
                      specialist_connection=specialist_connection, native_chat_connection=native_chat_connection,
                      pi_native_connection=pi_native_connection, hermes_native_connection=hermes_native_connection,
                      claude_native_connection=claude_native_connection, dsh_native_connection=dsh_native_connection,
                      dsh_root=args.native_dsh_history_root,
                      memory_program_drivers=memory_program_drivers,
                      **specialist_options)
    server = None
    try:
        if args.refresh or (not service.index_path.is_file() and not service.history_notice):
            try:
                service.refresh()
            except (HistorySourceError, OSError, sqlite3.Error):
                service.history_notice = '历史更新未完成，已有聊天仍可使用；来源稳定后可点击更新历史。'
        service.backends()
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        # Standard-library tracking joins every accepted Handler, including
        # maintenance controls that deliberately bypass business admission.
        server.daemon_threads = False
        server.block_on_close = True
        server.service = service
        print('一站式 AI 已启动：http://127.0.0.1:' + str(args.port), flush=True)

        def stop(signum, frame):
            def close_admission():
                service.admission.begin_shutdown()
                service.request_shutdown()
                server.shutdown()
            threading.Thread(target=close_admission, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()
    finally:
        service.admission.begin_shutdown()
        service.request_shutdown()
        if server is not None:
            server.server_close()
        # HTTP handlers have joined; keep the Service and its flock until
        # accepted background work also leaves its admission leases.
        while not service.admission.wait_for_idle(50):
            print('已停止接收新请求，正在等待已接收任务收尾。', flush=True)
        service.shutdown()


if __name__ == '__main__':
    main()
