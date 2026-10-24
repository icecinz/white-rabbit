#!/usr/bin/env python3
"""Private JSONL MCP bridge for one host-owned file-task directory.

No model calls, home discovery, shell execution or HTTP transport. The host
provides the root; MCP clients receive only the three bounded file tools.
"""
import argparse
import copy
import json
import math
import signal
import sys
import threading

from rabbit_content.file_task_files import FileTaskFiles, FileTaskError


TOOLS = ('rabbit_files_list', 'rabbit_file_read', 'rabbit_file_save')
FIELDS = {'rabbit_files_list': (), 'rabbit_file_read': ('path',),
          'rabbit_file_save': ('path', 'text')}
VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
# A 512 KiB UTF-8 file can expand sixfold in a JSON request; a read result
# includes text inside JSON content plus structuredContent (up to thirteenfold).
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_IDS = 4096


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _decode(raw):
    def no_constant(_):
        raise ValueError('invalid JSON number')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('invalid JSON number')
        return number
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                      parse_constant=no_constant, parse_float=finite_float)


def _string(value, maximum):
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        return False
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError:
        return False
    return True


def _error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id,
            'error': {'code': code, 'message': message}}


def _tool_error(code='invalid_arguments', message='工具参数无效。'):
    value = {'code': code, 'message': message}
    return {'content': [{'type': 'text', 'text': _json(value)}], 'isError': True}


class FileTaskMCP:
    def __init__(self, files):
        self.files = files
        self.state = 'new'
        self.version = None
        self.seen_ids = set()
        catalog = copy.deepcopy(files.tools())
        if (not isinstance(catalog, list) or len(catalog) != 3
                or {item.get('name') for item in catalog if isinstance(item, dict)} != set(TOOLS)):
            raise ValueError('file tool catalog unavailable')
        self.catalog = catalog
        self.schemas = {}
        for item in catalog:
            name, schema = item['name'], item.get('inputSchema')
            properties = schema.get('properties') if isinstance(schema, dict) else None
            required = schema.get('required', []) if isinstance(schema, dict) else None
            if (not isinstance(schema, dict) or schema.get('type') != 'object'
                    or schema.get('additionalProperties') is not False
                    or not isinstance(properties, dict) or set(properties) != set(FIELDS[name])
                    or not isinstance(required, list) or any(not isinstance(value, str) for value in required)
                    or len(required) != len(FIELDS[name]) or set(required) != set(FIELDS[name])
                    or any(not isinstance(field, dict) or field.get('type') != 'string'
                           for field in properties.values())):
                raise ValueError('file tool schema unavailable')
            for field in properties.values():
                low, high = field.get('minLength', 0), field.get('maxLength')
                if not (type(low) is int and type(high) is int and 0 <= low <= high <= MAX_LINE_BYTES):
                    raise ValueError('file tool schema unavailable')
            self.schemas[name] = schema
        if len(_json(catalog).encode('utf-8')) > 32 * 1024:
            raise ValueError('file tool catalog unavailable')

    def _arguments(self, name, value):
        if not isinstance(value, dict) or set(value) != set(FIELDS[name]):
            return False
        for field, rule in self.schemas[name]['properties'].items():
            text = value[field]
            if not isinstance(text, str):
                return False
            try:
                text.encode('utf-8', errors='strict')
            except UnicodeError:
                return False
            if len(text) < rule.get('minLength', 0) or len(text) > rule.get('maxLength', MAX_LINE_BYTES):
                return False
        return True

    def response(self, request):
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not _string(request.get('method'), 256)
                or not set(request).issubset({'jsonrpc', 'id', 'method', 'params'})):
            return _error(None, -32600, 'Invalid Request')
        # Notifications never execute file operations and never receive replies.
        if 'id' not in request:
            params = request.get('params', {})
            if (request['method'] == 'notifications/initialized' and self.state == 'initializing'
                    and isinstance(params, dict) and set(params).issubset({'_meta'})
                    and ('_meta' not in params or isinstance(params['_meta'], dict))):
                self.state = 'ready'
            return None
        request_id = request['id']
        if (not (type(request_id) is int and abs(request_id) <= 2 ** 53 - 1
                 or _string(request_id, 128))):
            return _error(None, -32600, 'Invalid Request')
        if request_id in self.seen_ids or len(self.seen_ids) >= MAX_REQUEST_IDS:
            return _error(request_id, -32600, 'Request id unavailable')
        self.seen_ids.add(request_id)
        params = request.get('params', {})
        if not isinstance(params, dict) or '_meta' in params and not isinstance(params['_meta'], dict):
            return _error(request_id, -32602, 'Invalid params')
        method = request['method']
        if method == 'ping':
            if not set(params).issubset({'_meta'}):
                return _error(request_id, -32602, 'Invalid params')
            result = {}
        elif method == 'initialize':
            if self.state != 'new':
                return _error(request_id, -32600, 'Already initialized')
            client = params.get('clientInfo')
            if (not set(params).issubset({'protocolVersion', 'capabilities', 'clientInfo', '_meta'})
                    or not _string(params.get('protocolVersion'), 32)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(client, dict)
                    or any(not _string(client.get(key), 256) for key in ('name', 'version'))):
                return _error(request_id, -32602, 'Invalid params')
            version = params['protocolVersion']
            self.version = version if version in VERSIONS else '2025-06-18'
            self.state = 'initializing'
            result = {'protocolVersion': self.version, 'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'rabbit-file-task', 'version': '1.0.0'},
                'instructions': '只在宿主提供的本任务目录列出、读取或新建UTF-8 .md/.txt文件；不能覆盖已有文件。'}
        elif self.state != 'ready':
            return _error(request_id, -32000, 'Initialization required')
        elif method == 'tools/list':
            if not set(params).issubset({'_meta'}):
                return _error(request_id, -32602, 'Invalid params')
            result = {'tools': copy.deepcopy(self.catalog)}
        elif method == 'tools/call':
            if (not set(params).issubset({'name', 'arguments', '_meta'})
                    or params.get('name') not in TOOLS):
                return _error(request_id, -32602, 'Invalid params')
            name, arguments = params['name'], params.get('arguments', {})
            if not self._arguments(name, arguments):
                result = _tool_error()
            else:
                try:
                    value = self.files.call(name, arguments)
                    if not isinstance(value, dict):
                        raise ValueError('file tool returned invalid result')
                    text = _json(value)
                    if len(text.encode('utf-8')) > MAX_RESPONSE_BYTES // 2:
                        raise ValueError('file tool result too large')
                    result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
                    if self.version in ('2025-06-18', '2025-11-25'):
                        result['structuredContent'] = value
                except FileTaskError as error:
                    result = _tool_error(error.code, str(error))
                except Exception:
                    result = _tool_error('file_task_unavailable', '本机文件任务暂不可用。')
        else:
            return _error(request_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def serve(bridge, input_stream, output_stream):
    while True:
        line = input_stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            while line and not line.endswith(b'\n'):
                line = input_stream.readline(MAX_LINE_BYTES + 1)
            response = _error(None, -32700, 'Request too large')
        else:
            try:
                response = bridge.response(_decode(line))
            except (ValueError, TypeError, UnicodeError, RecursionError):
                response = _error(None, -32700, 'Parse error')
        if response is not None:
            encoded = _json(response).encode('utf-8')
            if len(encoded) > MAX_RESPONSE_BYTES:
                encoded = _json(_error(response.get('id'), -32603, 'Response too large')).encode('utf-8')
            output_stream.write(encoded + b'\n')
            output_stream.flush()


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Invalid file-task bridge arguments.\n')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--root', required=True, help='Host-owned task directory')
    args = parser.parse_args(argv)
    cancel = threading.Event()

    def stop(number, _frame):
        cancel.set()
        raise SystemExit(128 + number)

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    try:
        # FileTaskFiles checks the host's .rabbit-cancelled marker at startup
        # and on every operation; already saved files are not rolled back.
        with FileTaskFiles(args.root, cancel_event=cancel) as files:
            serve(FileTaskMCP(files), sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        sys.stderr.write('File-task bridge unavailable.\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
