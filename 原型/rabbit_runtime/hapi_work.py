"""Optional HAPI list reader: Hub reports, never native executor ownership.

Contract: tiann/hapi v0.30.7 shared/src/sessionSummary.ts. Credentials belong
to the caller; no login, refresh, history, actions, cache or background loop.
"""
from datetime import datetime, timezone
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import quote


MAX_BYTES = 2 * 1024 * 1024
MAX_WORKS = 500
TIMEOUT_SECONDS = 3
_ORIGIN = re.compile(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z')


class HapiWorkError(ValueError):
    """Fixed public code; never includes a response, credential or exception text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _port(origin):
    match = _ORIGIN.fullmatch(origin) if type(origin) is str else None
    if not match or int(match[1]) > 65535:
        raise HapiWorkError('literal_loopback_origin_required')
    return int(match[1])


def _text(value, limit, *, required=False):
    if value is None and not required:
        return None
    if (type(value) is not str or len(value) > limit
            or required and not value
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        raise HapiWorkError('invalid_response')
    return value


def _number(value, *, counter=False):
    if (type(value) not in (int, float)
            or not 0 <= value <= (1_000_000 if counter else 2**53 - 1)
            or counter and type(value) is not int):
        raise HapiWorkError('invalid_response')
    return value


def project_sessions(payload, origin):
    """Pure bounded projection. Unknown fields are discarded, never traversed."""
    _port(origin)
    if type(payload) is not dict or type(payload.get('sessions')) is not list:
        raise HapiWorkError('invalid_response')
    if len(payload['sessions']) > MAX_WORKS:
        raise HapiWorkError('response_limit')
    works, identities = [], set()
    for item in payload['sessions']:
        if type(item) is not dict:
            raise HapiWorkError('invalid_response')
        identity = _text(item.get('id'), 256, required=True)
        if identity in identities or identity in ('.', '..'):
            raise HapiWorkError('invalid_response')
        identities.add(identity)
        metadata = item.get('metadata')
        if metadata is not None:
            if type(metadata) is not dict:
                raise HapiWorkError('invalid_response')
            metadata = {key: _text(metadata.get(key), limit) for key, limit in
                        (('name', 512), ('path', 4096), ('flavor', 64), ('agentSessionId', 256))}
        if type(item.get('active')) is not bool or type(item.get('thinking')) is not bool:
            raise HapiWorkError('invalid_response')
        row = dict(id=identity, metadata=metadata, active=item['active'], thinking=item['thinking'])
        for key in ('activeAt', 'updatedAt', 'pendingRequestsCount', 'backgroundTaskCount'):
            row[key] = _number(item.get(key), counter=key.endswith('Count'))
        for key in ('model', 'modelReasoningEffort', 'effort'):
            row[key] = _text(item.get(key), 256)
        row['reported_status'] = ('inactive' if not row['active'] else
            'needs_user' if row['pendingRequestsCount'] else
            'thinking' if row['thinking'] else 'active')
        row['url'] = origin + '/sessions/' + quote(identity, safe='')
        works.append(row)
    return works


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HapiWorkError('invalid_response')
        result[key] = value
    return result


def _constant(_value):
    raise HapiWorkError('invalid_response')


def _expire(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class HapiWorkReader:
    """One explicit loopback request per snapshot; no redirects/proxies/retries."""
    def __init__(self, origin, token):
        self._port = _port(origin)
        if (type(token) is not str or not 1 <= len(token) <= 8192
                or any(not 33 <= ord(c) <= 126 for c in token)):
            raise HapiWorkError('memory_bearer_token_required')
        self.origin, self._token = origin, token

    def snapshot(self):
        report = dict(schema_version=1, provider='hapi', scope='hapi_hub_summary', origin=self.origin,
            snapshot_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            native_event_freshness='unknown', native_executor_verified=False,
            capabilities={'send': False, 'approve': False, 'stop': False},
            complete=False, reason=None, works=[])
        deadline = time.monotonic() + TIMEOUT_SECONDS
        connection = http.client.HTTPConnection('127.0.0.1', self._port, timeout=TIMEOUT_SECONDS)
        timer = None
        try:
            connection.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # Keep the original socket: Connection: close can detach connection.sock.
            timer = threading.Timer(remaining, _expire, args=(connection.sock,))
            timer.daemon = True
            timer.start()
            connection.request('GET', '/api/sessions', headers={
                'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            if time.monotonic() >= deadline:
                raise TimeoutError
            if response.status in (401, 403):
                raise HapiWorkError('auth_required')
            if 300 <= response.status < 400:
                raise HapiWorkError('redirect_refused')
            if response.status != 200:
                raise HapiWorkError('http_error')
            if (response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json'
                    or response.getheader('Content-Encoding', 'identity').lower() != 'identity'):
                raise HapiWorkError('invalid_response')
            raw = response.read(MAX_BYTES + 1)
            if time.monotonic() >= deadline:
                raise TimeoutError
            if len(raw) > MAX_BYTES:
                raise HapiWorkError('response_limit')
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
            report['works'] = project_sessions(payload, self.origin)
            report['complete'] = True
        except HapiWorkError:
            raise
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            raise HapiWorkError('invalid_response') from None
        except (OSError, http.client.HTTPException):
            raise HapiWorkError('connection_unavailable') from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()
            if timer is not None:
                timer.join()
        return report
