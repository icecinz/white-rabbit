#!/usr/bin/env python3
"""Offline standalone content/runtime source, wheel and sdist checks.

All build/install outputs and fictional stores live in a temporary directory.
Consumers have no site packages and reject the other core package, application
modules, network and native process creation. This is packaging evidence only.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
from importlib.metadata import PackageNotFoundError, version
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('rabbit_content', 'rabbit_runtime')
CONSUMER = r'''
import importlib.abc
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

site, package = Path(sys.argv[1]).resolve(), sys.argv[2]
sys.path.insert(0, str(site))
class OnlyThisCore(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split('.')[0]
        if root != package and root not in sys.stdlib_module_names:
            raise AssertionError('Unexpected dependency: ' + fullname)
sys.meta_path.insert(0, OnlyThisCore())
assert sys.flags.isolated and sys.flags.no_site

with patch.object(Path, 'home', side_effect=AssertionError('Implicit HOME lookup')), \
        patch('subprocess.Popen', side_effect=AssertionError('Native process creation')), \
        patch('socket.socket', side_effect=AssertionError('Network request')):
    core = __import__(package)
    assert all(getattr(core, name) is not None for name in core.__all__)
    data = Path.cwd() / 'fictional-data'
    data.mkdir()
    checks = {'all_public_imports': True}
    if package == 'rabbit_content':
        from rabbit_content import FileTaskStore, MarkdownChatRegistry
        from rabbit_content.markdown_chat_document import render
        from rabbit_content.markdown_root_reconfirmation import root_reconfirmation_snapshot, reconfirm_root
        from rabbit_content import _directory_identity as identity
        assert callable(root_reconfirmation_snapshot) and callable(reconfirm_root)
        with FileTaskStore(data / 'files') as store:
            source = store.add_source('fictional.txt', 'fictional source', {'kind': 'upload'}, str(uuid4()))
            assert store.input_snapshot(source['id'], expected_sha256=source['sha256'])['text'] == 'fictional source'
        workspace = 'ab' * 32
        documents = data / 'documents'
        (documents / 'unfiled').mkdir(parents=True)
        snapshot = {'chat': {'id': str(uuid4()), 'title': 'Fictional standalone chat',
                    'revision': '0', 'source': None}, 'revision': '0', 'messages': [], 'run': None,
                    'markdown_projection': {'schema_version': 1, 'frozen_file_inputs_complete': True}}
        document = render(snapshot, workspace)
        registry = MarkdownChatRegistry(data / 'registry', documents, workspace_id=workspace)
        client = str(uuid4())
        try:
            prepared = registry.prepare(document, 'unfiled/chat.md')
            receipt = registry.confirm(prepared['publication']['id'], client)
            assert receipt['state'] == 'verified'
            bindings = registry.bindings()
            assert len(bindings) == 1
        finally:
            registry.close()
        registry = MarkdownChatRegistry(data / 'registry', documents, workspace_id=workspace)
        try:
            assert registry.find_receipt(client) == receipt
            assert registry.bindings() == bindings
            assert registry.scan({snapshot['chat']['id']: '0'})['chats'][0]['status'] == 'current'
            assert (documents / 'unfiled/chat.md').read_bytes() == document['data']
        finally:
            registry.close()
        assert not any(n == 'rabbit_runtime' or n.startswith('rabbit_runtime.') for n in sys.modules)
        checks.update(file_snapshot=True, markdown_publish_and_reopen=True, runtime_not_loaded=True)
    else:
        from rabbit_runtime import directory_identity as identity
        from rabbit_runtime import NativeSessionStore
        with NativeSessionStore(data / 'native.sqlite') as store:
            assert not any(store.maintenance_state()['counts'].values())
        assert not any(n == 'rabbit_content' or n.startswith('rabbit_content.') for n in sys.modules)
        checks.update(empty_runtime_ledger=True, content_not_loaded=True)
    anchor = identity.capture_directory(data)
    assert identity.validate_anchor(anchor) == anchor
    identity.verify_directory(data, anchor)
    loaded = [Path(m.__file__).resolve() for n, m in sys.modules.items()
              if n == package or n.startswith(package + '.')]
    assert loaded and all(p.is_relative_to(site / package) for p in loaded)
    checks['persistent_identity_primitive'] = True
print(json.dumps({'passed': True, 'checks': checks, 'native_processes': 0, 'network_requests': 0}))
'''


def fingerprints(source):
    return {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source.rglob('*')) if p.is_file()
            and not any(part in ('__pycache__', 'build', 'dist') or part.endswith('.egg-info')
                        for part in p.relative_to(source).parts)}


def verify(root=ROOT, *, source_only=False):
    root = Path(root).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(
        'rabbit_core_mirror_check', root / '工具/sync_directory_identity.py')
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    mirror = check.check(root)  # Gate before any build or subprocess.
    if not source_only:
        try:
            ready = (importlib.util.find_spec('pip') is not None
                     and int(version('setuptools').split('.')[0]) >= 68)
        except (PackageNotFoundError, ValueError):
            ready = False
        if not ready:
            raise RuntimeError('Explicit wheel verification requires local pip and setuptools>=68 '
                               'with wheel build support. Prepare build tools before this offline check; '
                               '--source-only uses only the standard library.')
    sources = {name: root / '原型' / name for name in PACKAGES}
    before = {name: fingerprints(path) for name, path in sources.items()}
    env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(name, None)
    env.update(PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONDONTWRITEBYTECODE='1')

    def run(command, cwd):
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Standalone package check failed:\n' + result.stdout[-1500:] + result.stderr[-5000:])
        return result.stdout

    def build(package, output, mode):
        output.mkdir()
        run([sys.executable, '-B', '-I', '-c',
             'import sys; from setuptools import build_meta; '
             'print(getattr(build_meta, sys.argv[1])(sys.argv[2]))',
             'build_' + mode, str(output)], package)
        built = list(output.glob('*.whl' if mode == 'wheel' else '*.tar.gz'))
        assert len(built) == 1, (mode, built)
        return built[0]

    def consume(site, package, cwd):
        cwd.mkdir()
        return json.loads(run([sys.executable, '-B', '-I', '-S', '-c',
                               CONSUMER, str(site), package], cwd))

    def install_and_consume(wheel, package, prefix):
        site = prefix / 'site'
        site.mkdir(parents=True)
        run([sys.executable, '-B', '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
             '--no-compile', '--no-cache-dir', '--target', str(site), str(wheel)], prefix)
        assert {p.name for p in site.iterdir() if p.is_dir() and not p.name.endswith('.dist-info')} == {package}
        return consume(site, package, prefix / 'consumer')

    reports = {}
    with tempfile.TemporaryDirectory(prefix='rabbit-standalone-cores-',
                                     dir=Path(tempfile.gettempdir()).resolve()) as temporary:
        temporary = Path(temporary)
        for name, source in sources.items():
            stage = temporary / name
            package = stage / 'source' / name
            shutil.copytree(source, package,
                            ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', 'build', 'dist'))
            project = tomllib.loads((package / 'pyproject.toml').read_text())['project']
            assert project['dependencies'] == []
            source_result = consume(package.parent, name, stage / 'source-consumer')
            if source_only:
                reports[name] = {'version': project['version'], 'source': source_result}
                continue
            wheel = build(package, stage / 'wheels', 'wheel')
            with zipfile.ZipFile(wheel) as archive:
                metadata = Parser().parsestr(archive.read(next(n for n in archive.namelist()
                    if n.endswith('.dist-info/METADATA'))).decode())
                assert metadata['Name'] == project['name'] and metadata['Version'] == project['version']
                assert all('; extra ==' in item for item in metadata.get_all('Requires-Dist', []))
                assert {n for n in archive.namelist() if n.endswith('.py')} == {
                    name + '/' + n for n in before[name] if n.endswith('.py')}
                if name == 'rabbit_content':
                    assert archive.read(name + '/_directory_identity.py') == (root / check.CANONICAL).read_bytes()
            wheel_result = install_and_consume(wheel, name, stage / 'wheel-install')
            sdist = build(package, stage / 'sdist', 'sdist')
            unpacked = stage / 'unpacked'
            unpacked.mkdir()
            with tarfile.open(sdist) as archive:
                for member in archive.getmembers():
                    relative = Path(member.name)
                    assert not relative.is_absolute() and '..' not in relative.parts
                    assert member.isdir() or member.isfile()
                    target = unpacked / relative
                    if member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.extractfile(member).read())
            unpacked_package, = unpacked.iterdir()
            rebuilt = build(unpacked_package, stage / 'sdist-wheel', 'wheel')
            sdist_result = install_and_consume(rebuilt, name, stage / 'sdist-install')
            reports[name] = {'version': project['version'], 'source': source_result,
                             'wheel': wheel_result, 'sdist_wheel': sdist_result,
                             'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                             'sdist_sha256': hashlib.sha256(sdist.read_bytes()).hexdigest()}
        assert {name: fingerprints(path) for name, path in sources.items()} == before
    return {'passed': True, 'mirror': mirror, 'packages': reports,
            'mode': 'source_only' if source_only else 'source_wheel_sdist',
            'offline': True, 'consumers_isolated_no_site': True,
            'source_unchanged': True, 'temporary_data_removed': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source-only', action='store_true',
                        help='Run isolated stdlib source consumers without pip or setuptools.')
    args = parser.parse_args(argv)
    result = verify(args.root, source_only=args.source_only)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output is not None:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
