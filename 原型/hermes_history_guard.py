"""Read-only pre-resume observations for an already verified Hermes lineage.

Pinned Hermes stores interrupted prompts in desktop/interrupted_turns.json and
leases in runtime/active_sessions.json. Its snapshot helper creates a lock and
can prune the registry, so it is deliberately not imported or invoked here.
This observation is not a lease; a new marker/owner can appear after return.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from rabbit_runtime.hermes.native_bootstrap import native_revision
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path


# Native turn_marker.py caps 32 prompts at 64,000 characters. Native json.dump
# defaults to ASCII escaping (up to 12 bytes for one non-BMP character).
MAX_MARKER_BYTES = 32 * 64000 * 12 + 65536
MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_OWNER_ENTRIES = 4096
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_SESSION = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254}[A-Za-z0-9])?\Z')
_MISSING = object()


class HermesHistoryGuardError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)  # Fixed codes only, never native file values.


def _fail(reason):
    raise HermesHistoryGuardError(reason)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_field')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('nonfinite_number')


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('nonfinite_number')
    return parsed


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _directory_id(path):
    descriptor = _directory(path)
    try:
        return _identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _json_file(home, folder, filename, limit, kind):
    """Bounded same-FD read plus named-file/directory checks; no lock creation.

    Return parent identity for an intra-call fence only. File inode/hash/mtime
    must not enter semantic proof: Hermes atomically replaces these JSON files
    for ordinary activity in unrelated sessions.
    """
    parent_path, parent, descriptor = home / folder, None, None
    try:
        try:
            parent = _directory(parent_path)
        except FileNotFoundError:
            try:
                check = _directory(parent_path)
            except FileNotFoundError:
                return _MISSING, None
            else:
                os.close(check)
                _fail('guard_scope_changed')
        parent_id = _identity(os.fstat(parent))
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            try:
                os.stat(filename, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if _directory_id(parent_path) != parent_id:
                    _fail('guard_scope_changed')
                return _MISSING, parent_id
            _fail('guard_scope_changed')
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail(kind + '_invalid')
        if before.st_size > limit:
            _fail(kind + '_too_large')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        after = os.fstat(descriptor)
        named = os.stat(filename, dir_fd=parent, follow_symlinks=False)
        if (_stamp(before) != _stamp(after) or _stamp(before) != _stamp(named)
                or len(raw) != before.st_size or _directory_id(parent_path) != parent_id):
            _fail('guard_scope_changed')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                               parse_constant=_constant, parse_float=_float)
        except (ValueError, UnicodeError, RecursionError):
            _fail(kind + '_invalid')
        return value, parent_id
    except HermesHistoryGuardError:
        raise
    except OSError:
        _fail(kind + '_unavailable')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _process_probe(pid, expected_start):
    """True/False/None using process metadata only; no signal or subprocess.

    Matches native active_sessions.py's 1ms PID/create-time comparison. psutil
    is optional and imported only for a matching lease; unavailable is unknown.
    A timestamp's age alone never establishes staleness.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        # Native gateway.status also treats zombies as dead. Status access is
        # best effort; a readable create time can still establish identity.
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        except Exception:
            pass
        current = process.create_time()
        if type(current) not in (int, float) or not math.isfinite(current):
            return None
        return True if expected_start is None else abs(current - expected_start) < .001
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return None


def _owner_entries(value, targets):
    if value is _MISSING:
        return []
    entries = value.get('entries') if type(value) is dict else value
    if type(entries) is not list or len(entries) > MAX_OWNER_ENTRIES:
        _fail('owner_invalid')
    selected, seen = [], set()
    for entry in entries:
        if type(entry) is not dict:
            _fail('owner_invalid')
        lease, session, pid = (entry.get(key) for key in ('lease_id', 'session_id', 'pid'))
        if (type(lease) is not str or not lease.strip() or len(lease) > 256
                or lease in seen or type(session) is not str or not session.strip() or len(session) > 256
                or type(pid) not in (int, str)):
            _fail('owner_invalid')
        seen.add(lease)
        try:
            pid = int(pid)  # Same int/string coercion as native _registry_pid.
        except ValueError:
            _fail('owner_invalid')
        if not 0 < pid <= (1 << 31) - 1:
            _fail('owner_invalid')
        for key, expected in (('surface', str), ('track_liveness', bool), ('metadata', dict)):
            if entry.get(key) is not None and type(entry[key]) is not expected:
                _fail('owner_invalid')
        start = entry.get('process_start_time')
        if start in (None, ''):
            start = None
        else:
            try:
                if type(start) not in (int, float, str):
                    raise ValueError
                start = float(start)
                if not math.isfinite(start):
                    raise ValueError
            except (ValueError, OverflowError):
                _fail('owner_invalid')
        if session in targets:
            # Hash the matching native lease only, retaining no prompt/metadata
            # or other session's identity in the returned proof.
            try:
                digest = _hash(entry)
            except (ValueError, UnicodeError, RecursionError):
                _fail('owner_invalid')
            selected.append((digest, pid, start))
    return sorted(selected)


class HermesHistoryGuard:
    def __init__(self, source_root, native_commit, *, process_probe=None):
        try:
            self.source_root = _path(source_root)
        except ValueError:
            _fail('invalid_arguments')
        if (type(native_commit) is not str or not _COMMIT.fullmatch(native_commit)
                or process_probe is not None and not callable(process_probe)):
            _fail('invalid_arguments')
        self.native_commit = native_commit
        self._probe = _process_probe if process_probe is None else process_probe

    def __call__(self, binding, chain):
        try:
            if type(binding) is not dict or type(chain) not in (list, tuple) or not 1 <= len(chain) <= 128:
                _fail('invalid_arguments')
            if (any(type(key) is not str or not _SESSION.fullmatch(key) for key in chain)
                    or len(set(chain)) != len(chain) or chain[0] != binding.get('session_key')):
                _fail('invalid_arguments')
            home, cwd = _path(binding['profile_home']), _path(binding['cwd'])
            expected_home = tuple(binding[key] for key in ('home_device', 'home_inode'))
            if any(type(value) is not int or value < 0 for value in expected_home):
                _fail('invalid_arguments')
            home_id, cwd_id, source_id = (_directory_id(path) for path in (home, cwd, self.source_root))
            if home_id != expected_home:
                _fail('guard_scope_changed')
            if 'cwd_device' in binding or 'cwd_inode' in binding:
                expected_cwd = tuple(binding[key] for key in ('cwd_device', 'cwd_inode'))
                if any(type(value) is not int or value < 0 for value in expected_cwd) or cwd_id != expected_cwd:
                    _fail('guard_scope_changed')
            if native_revision(self.source_root) != self.native_commit:
                _fail('native_revision_mismatch')
            targets = set(chain)

            def observe():
                marker, marker_parent = _json_file(home, 'desktop', 'interrupted_turns.json', MAX_MARKER_BYTES, 'marker')
                if marker is not _MISSING:
                    if type(marker) is not dict or len(marker) > 32:
                        _fail('marker_invalid')
                    # Presence alone is the gate: do not inspect, expose or
                    # normalize the interrupted prompt or decide to clear it.
                    if targets.intersection(marker):
                        _fail('interrupted_turn')
                owners, owner_parent = _json_file(home, 'runtime', 'active_sessions.json', MAX_OWNER_BYTES, 'owner')
                selected = _owner_entries(owners, targets)
                for _digest, pid, start in selected:
                    try:
                        state = self._probe(pid, start)
                    except Exception:
                        state = None
                    if state is True:
                        _fail('owner_active')
                    if state is not False:
                        _fail('owner_unknown')
                return [marker_parent, owner_parent], [value[0] for value in selected]

            first, owner_hashes = observe()
            second, current_hashes = observe()
            if first != second or owner_hashes != current_hashes:
                _fail('guard_scope_changed')
            if (home_id != _directory_id(home) or cwd_id != _directory_id(cwd)
                    or source_id != _directory_id(self.source_root)):
                _fail('guard_scope_changed')
            if native_revision(self.source_root) != self.native_commit:
                _fail('native_revision_mismatch')
            return {'native_revision': self.native_commit, 'home_identity': list(home_id),
                'cwd_identity': list(cwd_id), 'chain_sha256': _hash(list(chain)),
                'target_marker_present': False, 'target_owner_sha256': _hash(owner_hashes),
                'owner_observation': 'no_live_owner_observed', 'stale_target_leases': len(owner_hashes),
                'exclusive': False}
        except HermesHistoryGuardError:
            raise
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            _fail('guard_unavailable')
