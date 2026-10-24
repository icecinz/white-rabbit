"""Trusted Claude SDK/CLI startup paths; no imports, process or auth reads."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import uuid

from ..process import invoke, ProcessError
from .._version import VERSION
from .rpc import ClaudeRpc


# This one-shot helper imports only the already installed SDK path helpers.
# It does not initialize a CLI, Agent, session store, settings or provider.
_PROJECT_RESOLVER = '''
import importlib.metadata, json, os, re, sys
from pathlib import Path
if importlib.metadata.version('claude-agent-sdk') != sys.argv[1]:
    raise ValueError('SDK version mismatch')
from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir
cwd = _canonicalize_path(sys.argv[2])
encoded = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
if len(encoded) > 200 or any(ord(c) > 0xffff for c in cwd):
    raise ValueError('Unsupported project encoding')
configured = os.environ.get('CLAUDE_CONFIG_DIR')
if configured is not None and (not configured or not Path(configured).is_absolute()):
    raise ValueError('Unsupported config directory')
root = Path(configured) if configured is not None else Path.home()/'.claude'
project = _get_project_dir(cwd)
if project.parent != root/'projects':
    raise ValueError('Unsupported project directory')
print(json.dumps(dict(sdk=sys.argv[1], cwd=cwd, root=str(root), project_dir=str(project))))
'''


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


def _file(value):
    path = _path(value)
    if not path.is_file() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('Trusted executable is unavailable.')
    return path  # Preserve the lexical venv/CLI symlink, never switch installs.


def _directory(value):
    path = _path(value).resolve(strict=True)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('Trusted directory is unavailable.')
    return path


def session_uuid(value):
    if type(value) is not str:
        raise ValueError('An exact native session UUID is required.')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError('An exact native session UUID is required.') from None
    if str(parsed) != value:
        raise ValueError('An exact canonical session UUID is required.')
    return value


@dataclass(frozen=True)
class ClaudeNativeConnection:
    python: str | Path
    cli: str | Path
    expected_sdk: str = '0.2.152'
    expected_cli: str = '2.1.87'

    def __post_init__(self):
        object.__setattr__(self, 'python', _file(self.python))
        object.__setattr__(self, 'cli', _file(self.cli))
        if any(type(value) is not str or re.fullmatch(r'\d+\.\d+\.\d+', value) is None
               for value in (self.expected_sdk, self.expected_cli)):
            raise ValueError('Explicit installed SDK and CLI versions are required.')

    def create_rpc(self, *, cwd, session_id, resume=False):
        directory, identity = _directory(cwd), session_uuid(session_id)
        if type(resume) is not bool:
            raise ValueError('Resume must be explicit.')
        argv = [str(self.python), '-B', '-P', '-m', 'rabbit_runtime.claude.native_worker',
                '--cli', str(self.cli), '--cwd', str(directory),
                '--session-id', identity, '--mode', 'resume' if resume else 'new',
                '--expected-sdk', self.expected_sdk, '--expected-cli', self.expected_cli,
                '--expected-runtime', VERSION]
        return ClaudeRpc(argv, cwd=str(directory), env=dict(os.environ))

    def resolve_history_project(self, cwd, *, claude_root):
        """Resolve exact native transcript authority, without starting an Agent.

        The SDK must be the same fixed installation and inherit the same
        environment as create_rpc. Long/UTF-16-ambiguous project encodings are
        deliberately unsupported; this never performs prefix or latest lookup.
        """
        directory, root = _directory(cwd), _path(claude_root)
        frames = []
        try:
            invoke([str(self.python), '-B', '-P', '-c', _PROJECT_RESOLVER,
                    self.expected_sdk, str(directory)], dict(os.environ), None,
                   frames.append, timeout=8, cwd=directory, max_line=32768, max_output=32768)
        except ProcessError:
            raise ValueError('Original Claude history authority is unavailable.') from None
        if (len(frames) != 1 or set(frames[0]) != {'sdk', 'cwd', 'root', 'project_dir'}
                or frames[0]['sdk'] != self.expected_sdk or frames[0]['cwd'] != str(directory)
                or frames[0]['root'] != str(root)):
            raise ValueError('Original Claude history authority is unavailable.')
        project = _path(frames[0]['project_dir'])
        if project.parent != root/'projects':
            raise ValueError('Original Claude project does not match this source.')
        return project
