"""Trusted Hermes launch configuration; no native imports or startup here."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from .rpc import HermesRpc

NATIVE_SETTINGS_COMMIT = '145c713f1691b44bc485ce0ce4818cdb0e9c396b'


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
    try:
        if not path.is_file() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError('Trusted executable is unavailable.')
    except OSError:
        raise ValueError('Trusted executable is unavailable.') from None
    return path  # Keep lexical venv entry, including trusted executable symlinks.


def _directory(value):
    try:
        path = _path(value).resolve(strict=True)
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError('Trusted directory is unavailable.')
    except OSError:
        raise ValueError('Trusted directory is unavailable.') from None
    return path


@dataclass(frozen=True)
class HermesNativeConnection:
    node: str | Path
    python: str | Path
    source_root: str | Path
    home: str | Path | None = field(default=None, kw_only=True)
    session_settings_guard: bool = field(default=False, kw_only=True)
    native_commit: str = field(default=NATIVE_SETTINGS_COMMIT, kw_only=True)

    def __post_init__(self):
        if (type(self.session_settings_guard) is not bool or type(self.native_commit) is not str
                or re.fullmatch(r'[0-9a-f]{40}', self.native_commit) is None):
            raise ValueError('Invalid native settings guard configuration.')
        object.__setattr__(self, 'node', _file(self.node))
        object.__setattr__(self, 'python', _file(self.python))
        root = _directory(self.source_root)
        if not (root / 'hermes_cli/main.py').is_file():
            raise ValueError('Official Hermes source entry is unavailable.')
        object.__setattr__(self, 'source_root', root)
        if self.home is not None:
            object.__setattr__(self, 'home', _directory(self.home))

    def create_rpc(self, *, cwd, home=None):
        directory = _directory(cwd)
        selected = self.home if home is None else _directory(home)
        if self.home is not None and selected != self.home:
            raise ValueError('Explicit native home cannot be changed.')
        env = dict(os.environ)
        if selected is not None:
            env['HERMES_HOME'] = str(selected)
        worker = Path(__file__).parent / 'worker.mjs'
        argv = [str(self.node), str(worker), '--python', str(self.python), '--root', str(self.source_root)]
        if selected is not None and selected.parent.name != 'profiles':
            argv += ['--pin-default-profile', '1']
        if self.session_settings_guard:
            bootstrap = Path(__file__).parent / 'native_bootstrap.py'
            if not bootstrap.is_file():
                raise ValueError('Native session settings bootstrap is unavailable.')
            argv += ['--bootstrap', str(bootstrap), '--native-commit', self.native_commit]
        return HermesRpc(argv, cwd=str(directory), env=env)
