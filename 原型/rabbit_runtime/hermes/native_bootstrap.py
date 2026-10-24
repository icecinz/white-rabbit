"""Explicit optional startup extension; continues the installed Hermes CLI.

No import hook, native source replacement, alternate agent loop, or global install.
Only an owned loopback isolated serve invocation is accepted. Native profile/env
initialization runs before the added session-only RPC is registered.
"""
import argparse
import importlib
import importlib.util
from pathlib import Path
import re
import sys


_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_SAFE_REF = re.compile(r'refs/[A-Za-z0-9_./-]+\Z')


def _read(path, limit):
    with path.open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Native revision metadata exceeded its size bound.')
    return value.decode('ascii').strip()


def native_revision(root):
    """Read only local Git HEAD/ref metadata, with no Git process or network."""
    metadata = root / '.git'
    if metadata.is_file():
        marker = _read(metadata, 4096)
        if not marker.startswith('gitdir: '):
            raise ValueError('Native revision metadata is unavailable.')
        path = Path(marker[8:])
        metadata = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not metadata.is_dir():
        raise ValueError('Native revision metadata is unavailable.')
    head = _read(metadata / 'HEAD', 256)
    if _COMMIT.fullmatch(head):
        return head
    if not head.startswith('ref: '):
        raise ValueError('Native revision metadata is invalid.')
    ref = head[5:]
    if not _SAFE_REF.fullmatch(ref) or '..' in Path(ref).parts:
        raise ValueError('Native revision metadata is invalid.')
    common = metadata
    if (metadata / 'commondir').is_file():
        raw = Path(_read(metadata / 'commondir', 4096))
        common = (raw if raw.is_absolute() else metadata / raw).resolve(strict=True)
    for directory in (metadata, common):
        target = directory / ref
        if target.is_file():
            value = _read(target, 256)
            if not _COMMIT.fullmatch(value):
                raise ValueError('Native revision metadata is invalid.')
            return value
    packed = common / 'packed-refs'
    if packed.is_file():
        matches = [line.split(' ', 1)[0] for line in _read(packed, 4 * 1024 * 1024).splitlines()
                   if line.endswith(' ' + ref)]
        if len(matches) == 1 and _COMMIT.fullmatch(matches[0]):
            return matches[0]
    raise ValueError('Native revision metadata is unavailable.')


def _arguments(argv):
    if '--' not in argv:
        raise ValueError('The exact native CLI invocation is required.')
    split = argv.index('--')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-root', required=True)
    parser.add_argument('--expected-commit', required=True)
    options = parser.parse_args(argv[:split])
    root = Path(options.native_root)
    if (not root.is_absolute() or '..' in root.parts or not _COMMIT.fullmatch(options.expected_commit)
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in str(root))):
        raise ValueError('Invalid trusted native launch arguments.')
    root = root.resolve(strict=True)
    if not root.is_dir() or any(not (root / name).is_file()
            for name in ('hermes_cli/main.py', 'tui_gateway/server.py', 'hermes_constants.py')):
        raise ValueError('The trusted native source entry is unavailable.')
    native_argv = argv[split + 1:]
    tail = ['serve', '--host', '127.0.0.1', '--port', '0', '--isolated']
    if native_argv not in (tail, ['-p', 'default', *tail]):
        raise ValueError('Only the owned isolated loopback serve invocation is supported.')
    return root, options.expected_commit, native_argv


def _native_module(name, expected_file):
    module = importlib.import_module(name)
    if Path(getattr(module, '__file__', '')).resolve() != expected_file:
        raise ValueError('A native import resolved outside the trusted source entry.')
    return module


def main(argv=None):
    root, expected, native_argv = _arguments(list(sys.argv[1:] if argv is None else argv))
    if native_revision(root) != expected:
        raise ValueError('The installed Hermes revision differs from the explicit startup pin.')
    app_dir = Path(__file__).resolve().parent
    # Absolute native root wins. Empty/cwd and app paths must not shadow native
    # server.py, profiles.py, config modules, or their dependencies.
    sys.path[:] = [str(root), *(entry for entry in sys.path
        if entry and Path(entry).resolve() not in (app_dir, root))]
    sys.argv = [str(root / 'hermes_cli/main.py'), *native_argv]
    # Import performs the official -p pre-parse and dotenv setup before server
    # captures its native home. Do not run this module twice through runpy.
    cli = _native_module('hermes_cli.main', root / 'hermes_cli/main.py')
    server = _native_module('tui_gateway.server', root / 'tui_gateway/server.py')
    if native_revision(root) != expected:
        raise ValueError('The native revision changed during startup.')
    specification = importlib.util.spec_from_file_location(
        '_rabbit_hermes_session_settings', app_dir / 'session_settings.py')
    if specification is None or specification.loader is None:
        raise ValueError('The explicit session settings extension is unavailable.')
    extension = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(extension)
    extension.install_session_settings(server, installed_commit=expected, expected_commit=expected)
    queue_spec = importlib.util.spec_from_file_location(
        '_rabbit_hermes_image_queue', app_dir / 'image_queue.py')
    if queue_spec is None or queue_spec.loader is None:
        raise ValueError('The explicit image queue guard is unavailable.')
    queue_extension = importlib.util.module_from_spec(queue_spec)
    queue_spec.loader.exec_module(queue_extension)
    # _arguments above accepts only our private --isolated backend. Reuse the
    # exact session guard; do not introduce a second identity/idle definition.
    queue_extension.install_image_queue(server, installed_commit=expected, expected_commit=expected,
        identity_validator=extension._identity, live_validator=extension._live, private_backend=True)
    return cli.main()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Import/native errors can contain configuration values. The worker only
        # needs a startup failure; never print the raw exception or traceback.
        print('Rabbit Hermes session settings bootstrap failed.', file=sys.stderr)
        raise SystemExit(2)
