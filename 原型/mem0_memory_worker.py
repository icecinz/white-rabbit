"""Fixed optional Mem0 2.2.1 SDK worker. Import starts nothing."""
import contextlib
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import os
from pathlib import Path
import sys
import uuid

SDK_VERSION = '2.2.1'


def run(value):
    if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 1: raise ValueError()
    if version('mem0ai') != SDK_VERSION: raise ValueError()
    if value == {'schema_version': 1, 'action': 'probe'}:
        return {'schema_version': 1, 'available': True, 'sdk_version': SDK_VERSION}
    if set(value) != {'schema_version', 'action', 'profile_id', 'query', 'scope', 'config'} or value['action'] != 'recall':
        raise ValueError()
    identity = value['profile_id']
    if str(uuid.UUID(identity)) != identity: raise ValueError()
    scope = Path(value['scope'])
    if (not scope.is_absolute() or '..' in scope.parts or scope.name != identity or not scope.is_dir()
            or any(path.is_symlink() for path in (scope, *scope.parents))): raise ValueError()
    for name in ('sdk', 'qdrant', 'history.sqlite'):
        if (scope / name).is_symlink(): raise ValueError()
    config, query = value['config'], value['query']
    if type(query) is not str or not query.strip() or len(query) > 30000: raise ValueError()
    # Explicit API keys are passed to both factories. Ambient provider keys and
    # default ~/.mem0 state are never used by this worker.
    for key in tuple(os.environ):
        if key.startswith(('OPENAI_', 'MEM0_', 'QDRANT_')): os.environ.pop(key)
    os.environ.update(MEM0_TELEMETRY='false', MEM0_DIR=str(scope / 'sdk'))
    llm, embed = config['llm'], config['embedder']
    sdk_config = {
        'llm': {'provider': 'openai', 'config': {'model': llm['model'], 'api_key': llm['api_key'], 'openai_base_url': llm['base_url']}},
        'embedder': {'provider': 'openai', 'config': {'model': embed['model'], 'api_key': embed['api_key'],
            'openai_base_url': embed['base_url'], 'embedding_dims': embed['dimensions']}},
        'vector_store': {'provider': 'qdrant', 'config': {'path': str(scope / 'qdrant'),
            'collection_name': 'rabbit-' + identity, 'embedding_model_dims': embed['dimensions']}},
        'history_db_path': str(scope / 'history.sqlite'),
    }
    bank = 'rabbit-' + identity
    with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        logging.disable(logging.CRITICAL)
        from mem0 import Memory
        memory = Memory.from_config(sdk_config)
        result = memory.search(query, filters={'user_id': bank}, top_k=16, rerank=False)
    rows = result.get('results') if type(result) is dict else None
    if type(rows) is not list or len(rows) > 64: raise ValueError()
    safe = []
    for row in rows:
        if type(row) is not dict or row.get('user_id') != bank: raise ValueError()
        safe.append({key: row.get(key) for key in ('id', 'memory', 'user_id')})
    return {'schema_version': 1, 'sdk_version': SDK_VERSION, 'profile_id': identity,
            'query_sha256': hashlib.sha256(query.encode('utf-8')).hexdigest(), 'results': safe}


def main():
    try:
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024: raise ValueError()
        result = run(json.loads(raw.decode('utf-8')))
        output = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(output) > 192 * 1024: raise ValueError()
    except Exception:
        output = b'{"schema_version":1,"available":false,"reason":"mem0_unavailable"}'
    sys.stdout.buffer.write(output + b'\n')


if __name__ == '__main__':
    main()
