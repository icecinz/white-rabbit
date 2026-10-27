"""Trusted Pi argv only; preserve the user's native configuration and tools.

Restoring a missing file is unsafe: Pi otherwise silently creates a new session.
These checks are a local preflight, not a filesystem lock; the holder must also
verify get_state's exact sessionFile/sessionId before sending a prompt.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .rpc import PiRpc
from .material_protocol import MAX_RPC_FRAME_BYTES, MAX_RPC_EVENT_BYTES
from .profile_catalog import validate_startup_options


def _path(value):
    if not isinstance(value, (str, Path)):
        raise ValueError('A trusted absolute path is required.')
    text = str(value)
    if not text or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError('Invalid trusted path.')
    path = Path(text)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('A trusted absolute path is required.')
    return path


def _startup_file(value, *, executable):
    path = _path(value)
    try:
        if not path.is_file() or not os.access(path, os.X_OK if executable else os.R_OK):
            raise ValueError('Startup file is unavailable.')
    except OSError:
        raise ValueError('Startup file is unavailable.') from None
    return path


def _directory(value):
    path = _path(value)
    try:
        path = path.resolve(strict=True)
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError('Trusted directory is unavailable.')
    except OSError:
        raise ValueError('Trusted directory is unavailable.') from None
    return path


@dataclass(frozen=True)
class PiNativeConnection:
    node: str | Path
    cli_script: str | Path
    enable_task_results: bool = False

    def __post_init__(self):
        if type(self.enable_task_results) is not bool:
            raise ValueError('Task result observation requires an explicit boolean.')
        # Preserve the executable's lexical entry, including a trusted symlink.
        object.__setattr__(self, 'node', _startup_file(self.node, executable=True))
        object.__setattr__(self, 'cli_script', _startup_file(self.cli_script, executable=False))

    @property
    def task_results_supported(self):
        return self.enable_task_results

    def model_capabilities(self):
        """Construct the original SDK's bounded offline capability reader."""
        from .model_capabilities import PiModelCapabilities
        return PiModelCapabilities(self.node, self.cli_script)

    def create_rpc(self, *, cwd, session_dir, session_file=None, startup_overrides=None):
        overrides = validate_startup_options(startup_overrides)
        directory, sessions = _directory(cwd), _directory(session_dir)
        argv = [str(self.node)]
        if self.enable_task_results:
            argv += ['--import', str(Path(__file__).with_name('tracked_rpc_bootstrap.mjs'))]
        argv += [str(self.cli_script), '--mode', 'rpc', '--session-dir', str(sessions)]
        if session_file is not None:
            path = _path(session_file)
            try:
                # Check every component within the trusted directory before resolving.
                lexical_sessions = _path(session_dir)
                try:
                    relative, current = path.relative_to(lexical_sessions), lexical_sessions
                except ValueError:
                    relative, current = path.relative_to(sessions), sessions
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError('A restored session cannot use symlinks.')
                resolved = path.resolve(strict=True)
                if (not resolved.is_relative_to(sessions) or not stat.S_ISREG(path.stat().st_mode)
                        or not os.access(path, os.R_OK)):
                    raise ValueError('Restored session must be a readable owned regular file.')
            except (OSError, ValueError):
                raise ValueError('Restored session must be an existing nonsymlink file within session_dir.') from None
            argv += ['--session', str(resolved)]
        if overrides:
            argv += ['--provider', overrides['provider'], '--model', overrides['model']]
            if 'thinking' in overrides:
                argv += ['--thinking', overrides['thinking']]
        return PiRpc(argv, cwd=str(directory), max_frame_bytes=MAX_RPC_FRAME_BYTES,
                     max_event_bytes=MAX_RPC_EVENT_BYTES)
