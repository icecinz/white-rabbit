"""Manually bind one local HAPI Hub; credentials are prompted, never saved.

Uses HAPI v0.30.7's official /api/auth exchange. It does not start HAPI,
change its namespace, fetch sessions, or retry authentication or binding.
"""
import argparse
import getpass
import http.client
import json
import re
import socket
import sys
import threading

from rabbit_management_client import ManagementClient, ManagementClientError


_ORIGIN = re.compile(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z')
_JWT = re.compile(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z')
MAX_BYTES = 32 * 1024


class ConnectionError(ValueError):
    """A fixed public code, never the HTTP response or a supplied credential."""


def _port(origin):
    match = _ORIGIN.fullmatch(origin) if type(origin) is str else None
    if not match or int(match[1]) > 65535:
        raise ConnectionError('literal_loopback_origin_required')
    return int(match[1])


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConnectionError('invalid_response')
        result[key] = value
    return result


def _constant(_):
    raise ConnectionError('invalid_response')


def _json_request(port, path, body=None):
    """One fixed loopback request; no proxies, redirects, files or retries."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    timer = None
    def expire(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.connect()
        timer = threading.Timer(3, expire, args=(connection.sock,))
        timer.daemon = True
        timer.start()
        connection.request('GET' if body is None else 'POST', path,
            body=None if body is None else json.dumps(body).encode(),
            headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                     'Connection': 'close', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status in (401, 403):
            raise ConnectionError('authentication_failed')
        if response.status != 200:
            raise ConnectionError('http_rejected')
        if (response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json'
                or response.getheader('Content-Encoding', 'identity').lower() != 'identity'):
            raise ConnectionError('invalid_response')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ConnectionError('response_limit')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict:
            raise ConnectionError('invalid_response')
        return value
    except (OSError, http.client.HTTPException):
        raise ConnectionError('connection_unavailable') from None
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConnectionError('invalid_response') from None
    finally:
        connection.close()
        if timer is not None:
            timer.cancel()
            timer.join()


def exchange_token(origin, access_token):
    port = _port(origin)
    if (type(access_token) is not str or not 1 <= len(access_token) <= 8192
            or any(not 33 <= ord(c) <= 126 for c in access_token)):
        raise ConnectionError('invalid_access_token')
    response = _json_request(port, '/api/auth', {'accessToken': access_token})
    token = response.get('token')
    if type(token) is not str or not 1 <= len(token) <= 8192 or not _JWT.fullmatch(token):
        raise ConnectionError('invalid_response')
    return token


def local_client(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConnectionError('invalid_rabbit_port')
    context = _json_request(port, '/api/management/context')
    workspace = context.get('workspace_id')
    if type(workspace) is not str or not re.fullmatch(r'[a-f0-9]{64}', workspace):
        raise ConnectionError('workspace_unavailable')
    return ManagementClient(port=port, workspace_id=workspace)


def bind(client, origin, access_token):
    before = client.external_work_status()
    token = exchange_token(origin, access_token)
    return client.configure_external_work('bind', before['revision'], origin=origin, bearer_token=token)


def main(argv=None):
    parser = argparse.ArgumentParser(description='手动连接本机HAPI。密钥不回显、不写入文件；服务重启后重新连接。')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--origin', help='HAPI地址，例如 http://127.0.0.1:3006')
    mode.add_argument('--clear', action='store_true', help='仅清除小白兔内存中的HAPI连接')
    parser.add_argument('--rabbit-port', type=int, default=17860)
    args = parser.parse_args(argv)
    try:
        if args.origin:
            _port(args.origin)  # Reject unsupported destinations before asking for a key.
        client = local_client(args.rabbit_port)
        if args.clear:
            before = client.external_work_status()
            client.configure_external_work('clear', before['revision'])
            print('已清除小白兔中的HAPI连接。原HAPI会话保持原样。')
        else:
            if not sys.stdin.isatty():
                raise ConnectionError('interactive_terminal_required')
            secret = getpass.getpass('HAPI access token（原CLI_API_TOKEN，不回显）：')
            bind(client, args.origin, secret)
            secret = None
            print('已连接。到“管理 → Agent”点“刷新检测”查看HAPI报告的工作。')
        return 0
    except (ConnectionError, ManagementClientError) as error:
        print('连接未完成：' + str(error) + '。没有自动重试；如结果未知，请先读取当前连接。')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('已取消。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
