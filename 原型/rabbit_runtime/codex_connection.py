"""Trusted, lazy native Codex startup with the user's original configuration.

The ordinary connection adds no MCP server and requires no management runtime.
The specialist connection reuses the path checks and lazy holder construction,
then adds its one explicit application-management extension separately.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .codex_rpc import CodexRpc
from .specialist_service import SpecialistService


_WORKSPACE_ID = re.compile(r'[0-9a-f]{64}\Z')


def validate_launch_file(value, *, executable, label='专员'):
    """Validate one trusted absolute readable or executable launch file."""
    if not isinstance(value, (str, Path)):
        raise ValueError(label + '启动文件需要可信的绝对路径。')
    text = str(value)
    if not text or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError(label + '启动文件路径无效。')
    path = Path(text)
    try:
        usable = (path.is_absolute() and '..' not in path.parts and path.is_file()
                  and os.access(path, os.X_OK if executable else os.R_OK))
    except OSError:
        usable = False
    if not usable:
        raise ValueError(label + '启动文件需要可用的绝对文件路径。')
    # Preserve lexical executable paths: resolving a venv Python loses its venv.
    return path


def validate_codex_command(value, *, label='专员'):
    """Validate the native default command or one explicit executable path."""
    if not isinstance(value, (str, Path)):
        raise ValueError('Codex命令需要默认命令或可信绝对可执行路径。')
    return 'codex' if str(value) == 'codex' else str(validate_launch_file(value, executable=True, label=label))


def validate_workspace(workspace_id, *, label='专员管理'):
    """Validate the exact workspace identity required by a native connection."""
    if type(workspace_id) is not str or not _WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError(label + '连接需要准确的工作区身份。')


def create_holder(argv, store, *, cwd, session_key, rpc_type, holder_type):
    """Freeze startup arguments now; instantiate RPC only on explicit open."""
    command = tuple(argv)
    directory = str(Path(cwd).resolve(strict=True))
    return holder_type(store, cwd=directory, session_key=session_key,
        rpc_factory=lambda: rpc_type(list(command), cwd=directory))


@dataclass(frozen=True, kw_only=True)
class CodexNativeConnection:
    codex_command: str | Path = 'codex'

    def __post_init__(self):
        object.__setattr__(self, 'codex_command', validate_codex_command(self.codex_command, label='Codex'))

    def command(self, workspace_id):
        """Return native app-server argv; inherit all original Codex settings."""
        validate_workspace(workspace_id, label='Codex原生')
        return [self.codex_command, 'app-server', '--listen', 'stdio://']

    def create(self, store, *, cwd, workspace_id, session_key):
        return create_holder(self.command(workspace_id), store, cwd=cwd,
            session_key=session_key, rpc_type=CodexRpc, holder_type=SpecialistService)
