"""Admission adapters for existing business entries and finite HTTP maintenance.

The gate owns admission only. These adapters neither retry a business request
nor stop an Agent. A maintenance commit still requires physical server cleanup.
"""
from functools import wraps
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import urlsplit

from admission_gate import AdmissionClosed, AdmissionBusy


ROOT = '/api/maintenance'
POST_ACTIONS = frozenset(('prepare', 'state', 'cancel', 'commit'))


def acquire_service_lock(data_dir):
    """Startup only: install.SH -> service.EX(NB), then release install.SH.

    A controller holds install.EX through stop/deploy/activation registration.
    An automatic replacement process must wait here, before any store/Agent
    initialization, rather than accept new work between commit and bootout.
    """
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    startup = os.open(Path(data_dir) / 'install.lock', flags, 0o600)
    owned = None
    try:
        identity = os.fstat(startup)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise ValueError('服务启动锁不是独立普通文件。')
        os.set_inheritable(startup, False)
        fcntl.flock(startup, fcntl.LOCK_SH)
        owned = os.open(Path(data_dir) / 'service.lock', flags, 0o600)
        identity = os.fstat(owned)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise ValueError('服务独占锁不是独立普通文件。')
        os.set_inheritable(owned, False)
        fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return owned
    except BaseException:
        if owned is not None:
            os.close(owned)
        raise
    finally:
        os.close(startup)


def admitted(kind):
    """Optional for standalone protocol objects; Service supplies its own gate."""
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            gate = getattr(self, 'admission', None)
            if gate is None:
                return function(self, *args, **kwargs)
            with gate.lease(kind):
                return function(self, *args, **kwargs)
        return call
    return decorate


admitted_service = admitted('service')
admitted_native = admitted('native')
admitted_memory = admitted('memory')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate maintenance field.')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Nonfinite maintenance field.')


def maintenance_http(handler):
    """Only cached state/control can bypass admission, with normal origin/CSRF."""
    route = urlsplit(handler.path)
    if route.path != ROOT and not route.path.startswith(ROOT + '/'):
        return False
    if not handler._guard():
        return True
    service, gate = handler.service, handler.service.admission
    try:
        if route.query:
            raise ValueError('Unexpected query.')
        if handler.command == 'GET' and route.path == ROOT:
            handler._reply(200, {'maintenance': gate.state(), 'workspace_id': service.workspace_id,
                                  'csrf_token': service.csrf_token})
            return True
        action = route.path.removeprefix(ROOT + '/')
        if handler.command != 'POST' or action not in POST_ACTIONS:
            handler._reply(405, {'error': '请使用维护控制提供的操作。', 'code': 'maintenance_method'})
            return True
        if handler.headers.get_content_type() != 'application/json':
            raise ValueError('Expected JSON.')
        body = json.loads(handler._read_body(8192), object_pairs_hook=_pairs,
                          parse_constant=_invalid_constant, parse_float=_invalid_constant)
        keys = {'instance_id', 'maintenance_id', 'token', 'generation'}
        if type(body) is not dict or body.keys() - (keys | {'_one_ai_csrf'}) or not keys <= body.keys():
            raise ValueError('Invalid maintenance fields.')
        if '_one_ai_csrf' in body and type(body['_one_ai_csrf']) is not str:
            raise ValueError('Invalid maintenance nonce.')
        if not handler._allowed(mutate=True, nonce=body.get('_one_ai_csrf')):
            handler._reply(403, {'error': '维护消息校验未通过。', 'code': 'maintenance_denied'})
            return True
        instance_id = body['instance_id']
        if (type(instance_id) is not str or not secrets.compare_digest(
                instance_id.encode('utf-8'), gate.instance_id.encode('utf-8'))):
            handler._reply(409, {'error': '服务已经变化，请重新核对。', 'code': 'maintenance_instance_changed'})
            return True
        args = (body['maintenance_id'], body['token'], body['generation'])
        if action == 'prepare':
            state = gate.prepare(*args, probe=service.maintenance_probe)
        else:
            state = getattr(gate, action)(*args)
        handler._reply(200, {'maintenance': state})
    except (AdmissionClosed, AdmissionBusy) as error:
        handler._reply(409, {'error': '维护尚未就绪，请核对现有任务。', 'code': error.code,
                             'maintenance': error.state})
    except (ValueError, TypeError, UnicodeError, RecursionError):
        handler._reply(400, {'error': '维护控制格式无效。', 'code': 'maintenance_invalid'})
    return True


def admitted_http(function):
    """Hold admission through body read, business work and response writing."""
    @wraps(function)
    def call(handler, *args, **kwargs):
        if maintenance_http(handler):
            return
        try:
            with handler.service.admission.lease('http'):
                return function(handler, *args, **kwargs)
        except AdmissionClosed:
            # No accepted=False: a previous request with the same business ID
            # may still exist. This refusal describes only current admission.
            handler.close_connection = True
            handler._reply(503, {'error': '小白兔正在维护，草稿和原回执保留。',
                                 'code': 'maintenance_closed'})
    return call
