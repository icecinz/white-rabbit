"""Bounded offline access to the installed Pi SDK's original pure function.

Trusted Node/CLI paths come only from application startup configuration. No Pi
CLI, Agent, credential reader, or model request is executed. Only the SDK model
module and its static generated table are readable by the helper. Node 24 has
no network permission flag; the fixed helper disables standard network APIs.
This is not a general sandbox for running untrusted JavaScript modules.
"""
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import unicodedata


MAX_MODELS = 4096
MAX_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = MAX_BYTES + MAX_MODELS * 64
MAX_PROVIDER_CHARS = 256
MAX_MODEL_CHARS = 1024
LEVELS = frozenset(('off', 'minimal', 'low', 'medium', 'high', 'xhigh'))
_MESSAGES = {
    'installation_unavailable': 'Pi 原生能力函数或运行环境不可用。',
    'invalid_models': 'Pi 原生模型身份或能力元数据无效。',
    'capacity': 'Pi 原生模型能力查询超出容量。',
    'timeout': 'Pi 离线能力查询超时，未返回部分结果。',
    'helper_failed': 'Pi 离线能力查询未完成，未返回部分结果。',
    'invalid_response': 'Pi 离线能力结果不完整，未返回部分结果。',
    'source_changed': 'Pi 原生能力函数已变化，请重新核对安装。',
}


class PiModelCapabilitiesError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'helper_failed'
        super().__init__(_MESSAGES[self.code])


def _fail(code):
    raise PiModelCapabilitiesError(code)


def _file(value, *, executable=False):
    if not isinstance(value, (str, Path)):
        _fail('installation_unavailable')
    text = str(value)
    path = Path(text)
    if (not path.is_absolute() or '..' in path.parts or not text
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
        _fail('installation_unavailable')
    try:
        if not path.is_file() or not os.access(path, os.R_OK | (os.X_OK if executable else 0)):
            _fail('installation_unavailable')
        path.resolve(strict=True)
    except OSError:
        _fail('installation_unavailable')
    return path


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate key.')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _package(path, name):
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024 or _json(raw).get('name') != name:
            _fail('installation_unavailable')
    except (OSError, ValueError, AttributeError, UnicodeError):
        _fail('installation_unavailable')


def _stamp(path):
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            _fail('source_changed')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        _fail('source_changed')


def _identity(value, limit):
    # Mirror the full native catalogue's metadata boundary. Keep native spelling
    # and internal/edge whitespace; the separate startup codec decides whether
    # an identity can be represented safely as a literal CLI selection.
    if (type(value) is not str or not 1 <= len(value) <= limit or not value.strip()
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        _fail('invalid_models')
    try:
        value.encode('utf-8')
    except UnicodeError:
        _fail('invalid_models')
    return value


def _inputs(models):
    if type(models) is not list:
        _fail('invalid_models')
    if len(models) > MAX_MODELS:
        _fail('capacity')
    result, seen = [], set()
    for row in models:
        if type(row) is not dict or type(row.get('reasoning')) is not bool:
            _fail('invalid_models')
        provider = _identity(row.get('provider'), MAX_PROVIDER_CHARS)
        identity = _identity(row.get('id'), MAX_MODEL_CHARS)
        if (provider, identity) in seen:
            _fail('invalid_models')
        seen.add((provider, identity))
        item = {'provider': provider, 'id': identity, 'reasoning': row['reasoning']}
        if 'thinkingLevelMap' in row:
            mapping = row['thinkingLevelMap']
            if type(mapping) is not dict or any(key not in LEVELS for key in mapping):
                _fail('invalid_models')
            for value in mapping.values():
                if value is not None and (type(value) is not str or len(value) > 128
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    _fail('invalid_models')
            item['thinkingLevelMap'] = dict(mapping)
        result.append(item)
    try:
        raw = json.dumps({'schema_version': 1, 'models': result}, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, UnicodeError, RecursionError):
        _fail('invalid_models')
    if len(raw) > MAX_BYTES:
        _fail('capacity')
    return result, raw


class PiModelCapabilities:
    def __init__(self, node, cli_script, *, timeout=5):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            _fail('installation_unavailable')
        self.node, self.cli_script = _file(node, executable=True), _file(cli_script)
        self.timeout = float(timeout)
        cli = self.cli_script.resolve(strict=True)
        package = cli.parent.parent
        if cli.name != 'cli.js' or cli.parent.name != 'dist':
            _fail('installation_unavailable')
        package_file = package / 'package.json'
        _package(package_file, '@earendil-works/pi-coding-agent')
        # Follow Node's local/hoisted dependency lookup only within this trusted
        # installation. Never discover a path from an HTTP payload or model row.
        roots = [package]
        for parent in package.parents:
            roots.append(parent)
            if parent.name == 'node_modules':
                roots.append(parent.parent)
                break
        sdk_root = next((root / 'node_modules/@earendil-works/pi-ai' for root in roots
                         if (root / 'node_modules/@earendil-works/pi-ai/package.json').is_file()), None)
        if sdk_root is None:
            _fail('installation_unavailable')
        sdk_root = sdk_root.resolve(strict=True)
        sdk_package = sdk_root / 'package.json'
        _package(sdk_package, '@earendil-works/pi-ai')
        self.models_module = _file(sdk_root / 'dist/models.js').resolve(strict=True)
        generated = _file(sdk_root / 'dist/models.generated.js').resolve(strict=True)
        self.helper = _file(Path(__file__).resolve().parent / 'thinking_levels.mjs')
        self._readable = tuple(dict.fromkeys((self.helper, self.models_module, generated,
                                             package_file.resolve(strict=True), sdk_package)))
        self._sources = {path: _stamp(path) for path in self._readable}

    def levels(self, models):
        """Return [{provider, model, thinking_levels}], or fail as one query.

        The result uses the SDK's exact native levels. It does not clamp or map
        public ``none`` to native ``off``; that belongs to the execution adapter.
        """
        inputs, raw = _inputs(models)
        if any(_stamp(path) != stamp for path, stamp in self._sources.items()):
            _fail('source_changed')
        if not inputs:
            return []
        command = [str(self.node), '--permission', '--disable-proto=throw']
        command += ['--allow-fs-read=' + str(path) for path in self._readable]
        command += [str(self.helper), str(self.models_module)]
        # Do not inherit credential variables, HOME, NODE_OPTIONS, NODE_PATH,
        # preload hooks, or provider-specific configuration from the host.
        env = {'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC', 'NODE_NO_WARNINGS': '1'}
        try:
            completed = subprocess.run(command, input=raw, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=self.timeout,
                                       env=env, cwd=str(self.helper.parent), check=False)
        except subprocess.TimeoutExpired:
            _fail('timeout')
        except (OSError, ValueError):
            _fail('helper_failed')
        if any(_stamp(path) != stamp for path, stamp in self._sources.items()):
            _fail('source_changed')
        if (completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT_BYTES
                or len(completed.stderr) > MAX_BYTES):
            _fail('helper_failed')
        try:
            value = _json(completed.stdout.decode('utf-8'))
            if (type(value) is not dict or set(value) != {'schema_version', 'models'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or type(value['models']) is not list or len(value['models']) != len(inputs)):
                _fail('invalid_response')
            result = []
            for source, row in zip(inputs, value['models']):
                if (type(row) is not dict or set(row) != {'provider', 'model', 'thinking_levels'}
                        or row['provider'] != source['provider'] or row['model'] != source['id']
                        or type(row['thinking_levels']) is not list or len(row['thinking_levels']) > len(LEVELS)
                        or any(type(level) is not str or level not in LEVELS for level in row['thinking_levels'])
                        or len(set(row['thinking_levels'])) != len(row['thinking_levels'])):
                    _fail('invalid_response')
                result.append({'provider': row['provider'], 'model': row['model'],
                               'thinking_levels': list(row['thinking_levels'])})
            return result
        except (ValueError, UnicodeError, RecursionError, TypeError):
            _fail('invalid_response')
