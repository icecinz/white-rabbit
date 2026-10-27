"""Fictional loopback HTTP only; never HAPI, an Agent, user files or models."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from rabbit_runtime import hapi_work as hapi


TOKEN = 'fictional.jwt.token'
ORIGIN = 'http://127.0.0.1:3006'


def session(**changes):
    return dict(dict(id='fictional-one', metadata={'name': 'One', 'path': '/fictional/work',
        'flavor': 'codex', 'agentSessionId': 'native-one'}, active=True, thinking=False,
        activeAt=1000, updatedAt=2000, pendingRequestsCount=0, model='fixture-model',
        modelReasoningEffort='high', effort=None, backgroundTaskCount=0), **changes)


@contextmanager
def server(body=None, *, status=200, content_type='application/json', location=None, drip=None):
    records = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            records.append((self.command, self.path, dict(self.headers)))
            raw = body if type(body) is bytes else json.dumps(
                {'sessions': [session()]} if body is None else body).encode()
            if drip == 'headers':
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    for _ in range(100):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            if location:
                self.send_header('Location', location)
            self.end_headers()
            try:
                if drip == 'body':
                    for byte in raw:
                        self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.02)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_args):
            pass
    httpd = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01})
    worker.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', records
    finally:
        httpd.shutdown(); worker.join(3); httpd.server_close()


class ProjectionTests(unittest.TestCase):
    def project(self, row):
        return hapi.project_sessions({'sessions': [row]}, ORIGIN)[0]

    def test_minimal_fields_and_sensitive_unknowns_are_discarded(self):
        row = session(summary={'text': 'secret-body'}, messages=['private'], token='secret-token',
                      agentState={'requests': {'x': {'arguments': 'private-arguments'}}})
        row['metadata']['summary'] = {'text': 'private-summary'}
        row['metadata']['hapiMcpUrl'] = 'http://private-endpoint'
        before = deepcopy(row)
        result = self.project(row)
        self.assertEqual(row, before)
        self.assertEqual(set(result), {'id', 'metadata', 'active', 'thinking', 'activeAt', 'updatedAt',
            'pendingRequestsCount', 'backgroundTaskCount', 'model', 'modelReasoningEffort', 'effort',
            'reported_status', 'url'})
        self.assertEqual(set(result['metadata']), {'name', 'path', 'flavor', 'agentSessionId'})
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))

    def test_hub_states_and_unknown_flavor_are_not_native_claims(self):
        cases = [(dict(), 'active'), ({'thinking': True}, 'thinking'),
            ({'pendingRequestsCount': 2}, 'needs_user'), ({'backgroundTaskCount': 1}, 'active'),
            ({'active': False, 'thinking': True, 'pendingRequestsCount': 2}, 'inactive')]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.project(session(**changes))['reported_status'], expected)
        row = session(); row['metadata']['flavor'] = 'future-agent'
        self.assertEqual(self.project(row)['metadata']['flavor'], 'future-agent')
        self.assertIsNone(self.project(session(metadata=None))['metadata'])

    def test_url_has_no_token_and_encodes_opaque_id(self):
        result = self.project(session(id='id/?#% utf8兔'))
        self.assertEqual(result['url'], ORIGIN + '/sessions/id%2F%3F%23%25%20utf8%E5%85%94')
        self.assertNotIn(TOKEN, result['url'])

    def test_invalid_fields_fail_instead_of_guessing_idle(self):
        changes = [{'active': 1}, {'thinking': None}, {'pendingRequestsCount': True},
            {'backgroundTaskCount': -1}, {'activeAt': float('inf')}, {'updatedAt': '123'},
            {'id': '..'}, {'id': ''}, {'id': 'x\n'}, {'model': 'x' * 257},
            {'metadata': 'not-an-object'}, {'metadata': {'name': 'x' * 513}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.project(session(**change))
        for missing in ('active', 'thinking', 'pendingRequestsCount', 'backgroundTaskCount'):
            row = session(); del row[missing]
            with self.assertRaises(ValueError): self.project(row)

    def test_shape_count_and_duplicate_limits(self):
        for payload in (None, [], {}, {'sessions': {}}, {'sessions': [None]},
                        {'sessions': [session(), session()]},
                        {'sessions': [session(id=str(i)) for i in range(hapi.MAX_WORKS + 1)]}):
            with self.assertRaises(ValueError): hapi.project_sessions(payload, ORIGIN)
        self.assertEqual(hapi.project_sessions({'sessions': []}, ORIGIN), [])

    def test_malicious_origins_and_header_tokens_are_rejected(self):
        origins = ['https://127.0.0.1:3006', 'http://localhost:3006', 'http://127.1:3006',
            'http://2130706433:3006', 'http://[::1]:3006', 'http://0.0.0.0:3006',
            'http://127.0.0.1:3006/', 'http://127.0.0.1:3006?x', 'http://127.0.0.1:0',
            'http://127.0.0.1:65536', 'http://user@127.0.0.1:3006',
            'http://127.0.0.1:3006@evil.test', 'http://127.0.0.1:03006',
            'http://127.0.0.1:3006\n', None]
        for origin in origins:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                hapi.HapiWorkReader(origin, TOKEN)
        for token in ('', 'a\r\nX-Foo: injected', 'a b', 'x' * 8193, None):
            with self.assertRaises(ValueError): hapi.HapiWorkReader(ORIGIN, token)


class HttpTests(unittest.TestCase):
    def test_actual_single_get_fixed_path_no_proxy_and_no_control_capabilities(self):
        with server() as (origin, records), patch.dict(os.environ,
                {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            result = hapi.HapiWorkReader(origin, TOKEN).snapshot()
        self.assertTrue(result['complete'])
        self.assertEqual(result['scope'], 'hapi_hub_summary')
        self.assertEqual(result['native_event_freshness'], 'unknown')
        self.assertFalse(result['native_executor_verified'])
        self.assertEqual(result['capabilities'], {'send': False, 'approve': False, 'stop': False})
        self.assertEqual(len(records), 1)
        method, path, headers = records[0]
        self.assertEqual((method, path), ('GET', '/api/sessions'))
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(headers['Accept-Encoding'], 'identity')
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_auth_no_retry_and_failure_does_not_retain_old_idle(self):
        with server() as (origin, records):
            reader = hapi.HapiWorkReader(origin, TOKEN)
            self.assertTrue(reader.snapshot()['complete'])
        with self.assertRaises(hapi.HapiWorkError) as error:
            reader.snapshot()
        self.assertEqual(error.exception.code, 'connection_unavailable')
        for status in (401, 403):
            with server(b'private auth diagnostic', status=status) as (origin, records):
                with self.assertRaises(hapi.HapiWorkError) as error:
                    hapi.HapiWorkReader(origin, TOKEN).snapshot()
            self.assertEqual(error.exception.code, 'auth_required')
            self.assertEqual(len(records), 1)
            self.assertEqual(str(error.exception), 'auth_required')

    def test_redirect_is_not_followed_and_http_errors_do_not_echo_body(self):
        for code, reason in ((302, 'redirect_refused'), (500, 'http_error')):
            with server(b'private', status=code, location='http://127.0.0.1:1/private') as (origin, records):
                with self.assertRaises(hapi.HapiWorkError) as error:
                    hapi.HapiWorkReader(origin, TOKEN).snapshot()
            self.assertEqual(error.exception.code, reason)
            self.assertEqual(str(error.exception), reason)
            self.assertEqual(len(records), 1)

    def test_bad_json_wrong_type_and_oversize_are_refused(self):
        bodies = [b'not-json', b'{"sessions":[],"sessions":[1]}', b'{"sessions":NaN}',
            b'\xff', json.dumps({'sessions': [session(active=1)]}).encode(),
            b' ' * (hapi.MAX_BYTES + 1)]
        for raw in bodies:
            with server(raw) as (origin, records):
                with self.assertRaises(hapi.HapiWorkError) as error:
                    hapi.HapiWorkReader(origin, TOKEN).snapshot()
            self.assertEqual(error.exception.code,
                             'response_limit' if len(raw) > hapi.MAX_BYTES else 'invalid_response')
            self.assertEqual(str(error.exception), error.exception.code)
            self.assertEqual(len(records), 1)
        with server({'sessions': []}, content_type='text/html') as (origin, _):
            with self.assertRaises(hapi.HapiWorkError) as error:
                hapi.HapiWorkReader(origin, TOKEN).snapshot()
            self.assertEqual(error.exception.code, 'invalid_response')

    def test_empty_list_is_complete_without_claiming_external_agents_idle(self):
        with server({'sessions': []}) as (origin, records):
            result = hapi.HapiWorkReader(origin, TOKEN).snapshot()
        self.assertTrue(result['complete'])
        self.assertIsNone(result['reason'])
        self.assertEqual(result['works'], [])
        self.assertNotIn('status', result)
        self.assertEqual(len(records), 1)

    def test_total_deadline_stops_continuous_header_and_body_drips(self):
        for drip in ('headers', 'body'):
            with self.subTest(drip=drip), server({'sessions': []}, drip=drip) as (origin, records):
                with patch.object(hapi, 'TIMEOUT_SECONDS', .15):
                    started = time.monotonic()
                    with self.assertRaises(hapi.HapiWorkError) as error:
                        hapi.HapiWorkReader(origin, TOKEN).snapshot()
                    elapsed = time.monotonic() - started
                self.assertEqual(error.exception.code, 'connection_unavailable')
                self.assertLess(elapsed, .8)
                self.assertEqual(len(records), 1)


if __name__ == '__main__': unittest.main()
