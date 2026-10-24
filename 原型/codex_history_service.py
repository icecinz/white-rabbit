"""Codex's trusted source proof and specialist reservation; shared service shell."""
import fcntl
import os
from pathlib import Path
import stat

from codex_history_adoption import CodexHistoryAdoption
from rabbit_runtime.codex_connection import CodexNativeConnection
from codex_history_metadata import CodexHistoryMetadata
from native_history_service import NativeHistoryService, _failure
from rabbit_runtime.native_session_store import NativeSessionStore

ROOT = '/api/native-history/codex'


class CodexHistoryService(NativeHistoryService):
    def __init__(self, service, connection):
        self.service, self.manager = service, service.native_chats
        command = getattr(connection, 'codex_command', None)
        # Metadata must not attach the specialist's management host.
        metadata_connection = CodexNativeConnection(codex_command=command) if isinstance(command, (str, Path)) else connection
        reader = CodexHistoryMetadata(metadata_connection, workspace_id=service.workspace_id,
            codex_root=service.native_history_materials.codex_root)
        adoption = CodexHistoryAdoption(service.history,
            service.native_history_materials.codex_root, self.manager.catalogue, self.manager.store,
            reader, workspace_id=service.workspace_id, excluded_sources=service.native_chat_sources,
            reserved_sources=self._specialist_sources)
        super().__init__(service, self.manager, adoption, reader=reader)

    def _specialist_sources(self):
        result = []
        source = self.service._specialist_library_source()
        if source is not None:
            result.append(source)
        elif self.service._specialist_store is None:
            # A disabled specialist retains its fixed identity. Never adopt it
            # into an ordinary chat merely because its execution is disabled.
            path = self.service.data_dir / 'specialist-native.sqlite'
            guard = None
            if os.path.lexists(path):
                try:
                    guard = os.open(self.service.data_dir / '.specialist-native.sqlite.lock',
                                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    info = os.fstat(guard)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('Invalid specialist metadata lock.')
                    fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    with NativeSessionStore(path, readonly=True) as store:
                        binding = store.read_binding('specialist/codex')
                    if binding and binding['thread_id']:
                        result.append(('codex', binding['thread_id']))
                finally:
                    if guard is not None:
                        os.close(guard)
        return result

