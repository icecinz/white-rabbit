"""Trusted original DSH CLI/profile startup; no plugin or model startup here."""
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .rpc import DshRpc


def _path(value):
    if not isinstance(value, (str, Path)):
        raise ValueError('A trusted absolute path is required.')
    text = str(value)
    path = Path(text)
    if (not text or not path.is_absolute() or '..' in path.parts
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise ValueError('Invalid trusted absolute path.')
    return path


def _file(value, executable=False):
    path = _path(value)
    if not path.is_file() or not os.access(path, os.R_OK | (os.X_OK if executable else 0)):
        raise ValueError('Trusted startup file is unavailable.')
    return path


def _directory(value):
    path = _path(value).resolve(strict=True)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError('Trusted directory is unavailable.')
    return path


def validate_session_id(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None:
        raise ValueError('An exact native session ID is required.')
    return value


@dataclass(frozen=True)
class DshNativeConnection:
    node: str | Path
    cli_script: str | Path
    package_root: str | Path
    home: str | Path | None = None
    expected_version: str = '0.1.0-rc.6'

    def __post_init__(self):
        object.__setattr__(self, 'node', _file(self.node, True))
        object.__setattr__(self, 'cli_script', _file(self.cli_script))
        object.__setattr__(self, 'package_root', _directory(self.package_root))
        if self.cli_script.resolve() != (self.package_root / 'lib/bin.js').resolve():
            raise ValueError('The original CLI must belong to the pinned package root.')
        if self.home is not None:
            object.__setattr__(self, 'home', _directory(self.home))
        if type(self.expected_version) is not str or re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', self.expected_version) is None:
            raise ValueError('An explicit installed package version is required.')

    def create_rpc(self, *, cwd, session_id, agent_preset=None, expected_native_path=None):
        directory = _directory(cwd)
        identity = validate_session_id(session_id)
        if agent_preset is not None and (type(agent_preset) is not str or not agent_preset
                or len(agent_preset) > 256 or any(c.isspace() or ord(c) < 32 for c in agent_preset)):
            raise ValueError('An exact native preset or null is required.')
        worker = Path(__file__).with_name('worker.mjs')
        argv = [str(self.node), str(worker), '--cli', str(self.cli_script), '--root', str(self.package_root),
                '--session-id', identity, '--cwd', str(directory), '--version', self.expected_version]
        if agent_preset is not None:
            argv += ['--preset', agent_preset]
        if expected_native_path is not None:
            # Trusted registration proof only; the web request cannot choose a
            # path. The original backend must locate this same file before it
            # is allowed to cold-resume the old session.
            original = _path(expected_native_path)
            if len(str(original)) > 8192:
                raise ValueError('The original session path exceeds the native boundary.')
            argv += ['--expected-native-path', str(original)]
        env = dict(os.environ)
        if self.home is not None:
            env['DSH_HOME'] = str(self.home)
        return DshRpc(argv, cwd=str(directory), env=env)
