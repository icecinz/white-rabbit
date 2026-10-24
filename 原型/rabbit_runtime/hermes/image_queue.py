"""Read the owned native Hermes RAM attachment queue, without changing it.

The explicit isolated bootstrap supplies the existing settings identity/live
validators. Hermes still owns attachment staging, image routing and prompts.
This observation is not an atomic attach/submit transaction, and queue counts
cannot identify a lost-ACK request or authorize replay, detachment or discard.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
import uuid


METHOD = 'rabbit.image.queue'
SCHEMA_VERSION = 1
INSPECT_SCHEMA_VERSION = 2
QUEUE_SOURCE = 'native_session.attached_images'
MAX_QUEUED_IMAGES = 1024
MAX_QUEUE_ENTRY_BYTES = 16 * 1024
MAX_INSPECT_IMAGES = 4
MAX_INSPECT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INSPECT_TOTAL_BYTES = 20 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_SNAPSHOT = re.compile(r'(?P<sha256>[0-9a-f]{64})\.(?P<ext>png|jpg)\Z')
_IDENTITY_FIELDS = {'session_id', 'session_key', 'cwd', 'profile_home'}
_ERRORS = {
    'invalid_arguments': (4002, 'Invalid exact-session image queue request.'),
    'not_found': (4001, 'The exact native session is no longer live.'),
    'identity_changed': (4007, 'The native image queue identity changed.'),
    'not_ready': (5032, 'The native session agent is not ready.'),
    'busy': (4009, 'The native session has active or pending work.'),
    'queue_invalid': (5032, 'The native image queue cannot be verified.'),
}


class _QueueError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code][1])


def _error(rid, code):
    number, message = _ERRORS[code]
    return {'jsonrpc': '2.0', 'id': rid, 'error': {
        'code': number, 'message': message,
        'data': {'rabbit_code': 'image_queue_' + code}}}


def _queue(session):
    """Snapshot bounded strings only; never resolve paths or read image files."""
    value = session.get('attached_images')
    if type(value) is not list or len(value) > MAX_QUEUED_IMAGES:
        raise _QueueError('queue_invalid')
    snapshot = tuple(value)
    for entry in snapshot:
        if (type(entry) is not str or not entry or len(entry) > 4096
                or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                       for char in entry)
                or len(entry.encode('utf-8')) > MAX_QUEUE_ENTRY_BYTES):
            raise _QueueError('queue_invalid')
    return value, snapshot


def _scope(params):
    workspace, chat = params['workspace_id'], params['chat_id']
    if type(workspace) is not str or _WORKSPACE.fullmatch(workspace) is None:
        raise _QueueError('invalid_arguments')
    try:
        if type(chat) is not str or str(uuid.UUID(chat)) != chat:
            raise _QueueError('invalid_arguments')
    except (ValueError, TypeError, AttributeError):
        raise _QueueError('invalid_arguments') from None
    return workspace, chat


def _owned_paths(identity, workspace, chat, queue):
    """Authorize every queue member before ANY filesystem observation.

    The caller cannot supply paths. Only the real native queue and an exact
    profile/workspace/chat-owned directory can nominate files for inspection.
    """
    if len(queue) > MAX_INSPECT_IMAGES:
        raise _QueueError('queue_invalid')
    root = PurePosixPath(identity['profile_home']) / 'images' / 'rabbit-native' / workspace / chat / 'images'
    owned = []
    for entry in queue:
        path = PurePosixPath(entry)
        match = _SNAPSHOT.fullmatch(path.name)
        if str(path) != entry or path.parent != root or match is None:
            raise _QueueError('queue_invalid')
        owned.append((entry, path.name, match['sha256'], 'image/png' if match['ext'] == 'png' else 'image/jpeg'))
    return root, owned


def _file_version(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular_image(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o400
            or not 0 < info.st_size <= MAX_INSPECT_IMAGE_BYTES):
        raise _QueueError('queue_invalid')


def _inspect_files(root, owned):
    """Read only pinned descriptors, rechecking every file and path anchor.

    Registry ownership and the private holder's serialized actions protect the
    native session lifetime; these file checks do not turn unrelated native
    attachment writers into an atomic transaction.
    """
    if not owned:
        return []
    with ExitStack() as resources:
        def opened(path, flags, *, directory=None):
            descriptor = os.open(path, flags, **({} if directory is None else {'dir_fd': directory}))
            resources.callback(os.close, descriptor)
            return descriptor

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
        directory = opened(root.anchor, directory_flags)
        anchors = []
        for part in root.parts[1:]:
            child = opened(part, directory_flags, directory=directory)
            anchors.append((directory, part, child))
            directory = child
        files, total = [], 0
        # Stat the whole known set before reading any image payload.
        for path, name, wanted, mime in owned:
            descriptor = opened(name, file_flags, directory=directory)
            before = os.fstat(descriptor)
            _regular_image(before)
            total += before.st_size
            if total > MAX_INSPECT_TOTAL_BYTES:
                raise _QueueError('queue_invalid')
            files.append((path, name, wanted, mime, descriptor, before))
        result = []
        for path, name, wanted, mime, descriptor, before in files:
            digest, size, magic = hashlib.sha256(), 0, b''
            while size <= MAX_INSPECT_IMAGE_BYTES:
                raw = os.read(descriptor, min(_READ_CHUNK_BYTES, MAX_INSPECT_IMAGE_BYTES + 1 - size))
                if not raw:
                    break
                if len(magic) < 8:
                    magic += raw[:8 - len(magic)]
                size += len(raw)
                digest.update(raw)
            valid_magic = (magic.startswith(b'\x89PNG\r\n\x1a\n') if mime == 'image/png'
                           else magic.startswith(b'\xff\xd8\xff'))
            if (size != before.st_size or digest.hexdigest() != wanted or not valid_magic
                    or _file_version(os.fstat(descriptor)) != _file_version(before)):
                raise _QueueError('queue_invalid')
            result.append({'path': path, 'sha256': wanted, 'size_bytes': size, 'mime': mime})
        # A later image read may have raced a replacement of an earlier file.
        for _, name, _, _, descriptor, before in files:
            observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (_file_version(os.fstat(descriptor)) != _file_version(before)
                    or _file_version(observed) != _file_version(before)):
                raise _QueueError('queue_invalid')
        for parent, name, child in anchors:
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(child)
            if (not stat.S_ISDIR(observed.st_mode)
                    or (observed.st_dev, observed.st_ino) != (pinned.st_dev, pinned.st_ino)):
                raise _QueueError('queue_invalid')
        return result


def install_image_queue(server, *, installed_commit, expected_commit,
                        identity_validator, live_validator, private_backend):
    """Register once in an explicitly owned, isolated native backend.

    The bootstrap proves its exact ``serve --isolated`` invocation before
    passing ``private_backend=True``. The callbacks are the existing settings
    ``_identity`` and ``_live`` functions; no native imports or global hooks are
    needed here. The native registry lock is the only lock acquired. The
    original read/capabilities remain schema 1; explicit inspect responses use
    schema 2. Inspect still requires the private holder to serialize actions,
    because the native attach handler does not acquire this registry lock.
    """
    if (type(installed_commit) is not str or not _COMMIT.fullmatch(installed_commit)
            or type(expected_commit) is not str or installed_commit != expected_commit
            or private_backend is not True):
        raise ValueError('The owned native Hermes revision or backend did not match.')
    if (any(not hasattr(server, name) for name in ('_sessions', '_sessions_lock', '_methods', 'method'))
            or type(server._sessions) is not dict or type(server._methods) is not dict
            or not all(callable(value) for value in (server.method, identity_validator, live_validator))
            or METHOD in server._methods):
        raise ValueError('The native Hermes image queue interface is unavailable.')
    capabilities = {'schema_version': SCHEMA_VERSION, 'method': METHOD,
        'guard': 'exact_live_session', 'installed_commit': installed_commit,
        'actions': ['read'], 'private_backend': True, 'queue_source': QUEUE_SOURCE}

    def handler(rid, params):
        try:
            if type(params) is not dict:
                raise _QueueError('invalid_arguments')
            if params.get('action') == 'capabilities':
                if set(params) != {'action'}:
                    raise _QueueError('invalid_arguments')
                return {'jsonrpc': '2.0', 'id': rid, 'result': deepcopy(capabilities)}
            action = params.get('action')
            if action not in ('read', 'inspect'):
                raise _QueueError('invalid_arguments')
            fields = {'action', *_IDENTITY_FIELDS}
            if action == 'inspect':
                fields.update(('workspace_id', 'chat_id'))
            if set(params) != fields:
                raise _QueueError('invalid_arguments')
            scope = _scope(params) if action == 'inspect' else None
            identity = identity_validator(params)
            if (type(identity) is not dict or set(identity) != _IDENTITY_FIELDS
                    or any(identity[key] != params[key] for key in _IDENTITY_FIELDS)):
                raise _QueueError('invalid_arguments')
            with server._sessions_lock:
                session, agent = live_validator(server, identity)
                if (type(session) is not dict or agent is None
                        or server._sessions.get(identity['session_id']) is not session):
                    raise _QueueError('identity_changed')
                queue, before = _queue(session)
                images = None
                if action == 'inspect':
                    root, owned = _owned_paths(identity, *scope, before)
                    images = _inspect_files(root, owned)
                current_session, current_agent = live_validator(server, identity, expected=session)
                current_queue, after = _queue(current_session)
                if (current_session is not session or current_agent is not agent
                        or server._sessions.get(identity['session_id']) is not session
                        or current_queue is not queue or after != before):
                    raise _QueueError('identity_changed')
                if action == 'inspect':
                    return {'jsonrpc': '2.0', 'id': rid, 'result': {
                        'schema_version': INSPECT_SCHEMA_VERSION, **identity,
                        'workspace_id': scope[0], 'chat_id': scope[1], 'images': images}}
                return {'jsonrpc': '2.0', 'id': rid, 'result': {
                    'schema_version': SCHEMA_VERSION, **identity,
                    'queued_count': len(before), 'queue_empty': not before}}
        except _QueueError as error:
            return _error(rid, error.code)
        except Exception as error:
            # Validator errors are projected into this independent protocol;
            # unknown exceptions, paths and native diagnostics never escape.
            code = getattr(error, 'code', None)
            return _error(rid, code if type(code) is str and code in _ERRORS else 'queue_invalid')

    server.method(METHOD)(handler)
    if server._methods.get(METHOD) is not handler:
        raise ValueError('The native RPC registrar did not retain the image queue handler.')
    return deepcopy(capabilities)
