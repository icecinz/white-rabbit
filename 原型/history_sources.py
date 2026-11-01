"""The app owns one derived history index, independently of the legacy MCP.

Refreshes stage a full index; bootstrap copies a read-only legacy snapshot once.
No credential settings, source transcripts, or legacy indexes are modified here.
"""
import datetime as dt
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from functools import partial
from urllib.parse import quote

from claude_history import ingest_claude, META_KEY as CLAUDE_META_KEY
from pi_history import ingest_pi, META_KEY as PI_META_KEY


class HistorySourceError(ValueError):
    pass


_SOURCE_SIGNATURE_KEY = 'source_signature_v1'
_SOURCE_INCOMPLETE_KEY = 'source_incomplete_v1'


class _LimitedReader:
    def __init__(self, file, limit, *, digest=True):
        self.file, self.remaining = file, limit
        self.digest = hashlib.sha256() if digest else None

    def readline(self, size=-1):
        if self.remaining <= 0:
            return b''
        data = self.file.readline(min(size, self.remaining) if size >= 0 else self.remaining)
        self.remaining -= len(data)
        if self.digest is not None:
            self.digest.update(data)
        return data


class _CodexSnapshot:
    """Bounded original-file reads, checked again before publishing.

    The capture is per file, not a globally atomic point in time. Only the
    captured file set and complete lines are indexed; no source is modified.
    """
    def __init__(self, root, source_state):
        self.root, self.files, self.entries = root, [], {}
        self.incomplete = False
        now = time.time_ns()
        for relative, signature in source_state or ():
            if not (relative == 'session_index.jsonl' or
                    (relative.startswith(('sessions/', 'archived_sessions/')) and relative.endswith('.jsonl'))):
                continue
            if not signature:
                continue
            path = root / relative
            # Older files must stay entirely unchanged. A recent or future
            # timestamp may be an active writer and receives a prior digest.
            entry = {'identity': signature[:2], 'size': signature[2], 'signature': signature,
                     'active': now - signature[3] <= 300 * 1_000_000_000}
            with self._open(path, entry) as file:
                digest = self._hash(file, entry['size']) if entry['active'] else None
                last = os.pread(file.fileno(), 1, entry['size'] - 1) if entry['size'] else b'\n'
                limit = entry['size']
                if last != b'\n':
                    self.incomplete = True
                    while limit:
                        start = max(0, limit - 65536)
                        block = os.pread(file.fileno(), limit - start, start)
                        if len(block) != limit - start:
                            raise HistorySourceError('Codex来源读取不完整，现有索引保留。')
                        pos = block.rfind(b'\n')
                        if pos >= 0:
                            limit = start + pos + 1
                            break
                        limit = start
                entry.update(digest=digest, limit=limit, seen=False)
            self.entries[path] = entry
            if relative != 'session_index.jsonl':
                self.files.append(path)

    @contextlib.contextmanager
    def _open(self, path, entry):
        parent = _directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != entry['identity'] or st.st_size < entry['size']:
                    raise HistorySourceError('Codex来源已替换或截断，现有索引保留。')
                if not entry['active'] and _signature(st) != entry['signature']:
                    raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
                with os.fdopen(fd, 'rb') as file:
                    fd = None
                    yield file
            finally:
                if fd is not None:
                    os.close(fd)
        finally:
            os.close(parent)

    @staticmethod
    def _hash(file, size):
        file.seek(0); digest = hashlib.sha256(); remaining = size
        while remaining:
            data = file.read(min(1024 * 1024, remaining))
            if not data:
                raise HistorySourceError('Codex来源已截断，现有索引保留。')
            digest.update(data); remaining -= len(data)
        return digest.hexdigest()

    @contextlib.contextmanager
    def open(self, path):
        entry = self.entries[path]
        with self._open(path, entry) as file:
            # Inactive files must keep their complete original metadata;
            # verify rejects any change before consulting a digest. Hashing
            # their bytes would therefore never affect the result.
            reader = _LimitedReader(file, entry['limit'], digest=entry['active'])
            yield reader
            if reader.remaining:
                raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
            remaining = entry['size'] - entry['limit']
            while remaining:
                data = file.read(min(1024 * 1024, remaining))
                if not data:
                    raise HistorySourceError('Codex来源已截断，现有索引保留。')
                if reader.digest is not None:
                    reader.digest.update(data)
                remaining -= len(data)
            parsed = reader.digest.hexdigest() if reader.digest is not None else None
            if entry['digest'] is not None and parsed != entry['digest']:
                raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
            entry.update(digest=parsed, seen=True)

    def verify(self):
        for path, entry in self.entries.items():
            with self._open(path, entry) as file:
                current = _signature(os.fstat(file.fileno()))
                if not entry['seen'] or current[2] == entry['size'] and current != entry['signature']:
                    raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
                if current != entry['signature'] and self._hash(file, entry['size']) != entry['digest']:
                    raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
                entry['verified_signature'] = current

    def verify_paths(self, before, after):
        if before is None:
            return
        if after is None:
            raise HistorySourceError('Codex来源目录已变化，现有索引保留。')
        new = dict(after)
        for name, old in before:
            if old is None:
                continue
            current = new.get(name)
            if current is None or old[:2] != current[:2]:
                raise HistorySourceError('Codex来源已替换或删除，现有索引保留。')
            entry = self.entries.get(self.root / name)
            if entry and (current[2] < old[2] or current[2] == old[2] and current != old
                          or not entry['active'] and current != old):
                raise HistorySourceError('Codex来源原有内容已变化，现有索引保留。')
            if entry and current != entry['verified_signature']:
                with self._open(self.root / name, entry) as file:
                    if (_signature(os.fstat(file.fileno())) != current or
                            self._hash(file, entry['size']) != entry['digest'] or
                            _signature(os.fstat(file.fileno())) != current):
                        raise HistorySourceError('Codex来源核对期间变化，现有索引保留。')


def _source_signature(sources):
    # Paths and names contribute to identity but only the digest is stored.
    raw = [(str(path), tree, branches, state) for path, tree, branches, state in sources]
    return hashlib.sha256(json.dumps(raw, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _path(value):
    return Path(os.path.abspath(Path(value).expanduser()))


def _signature(item):
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _directory(path, create=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file_state(directory, name):
    try:
        item = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(item.st_mode):
        raise HistorySourceError('历史索引或来源必须是普通文件，不能是符号链接。')
    return _signature(item)


def _source_state(path, tree=False, branches=()):
    """Only metadata of the explicitly selected source is inspected."""
    try:
        if not tree:
            parent = _directory(path.parent)
            try:
                values = [(_file_state(parent, path.name), path.name)]
                for suffix in ('-wal', '-shm', '-journal'):
                    values.append((_file_state(parent, path.name + suffix), suffix))
                return tuple(values)
            finally:
                os.close(parent)
        root = _directory(path)
    except FileNotFoundError:
        return None
    result = [('.', _signature(os.fstat(root)))]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    def visit(fd, prefix):
        for name in sorted(os.listdir(fd)):
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(item.st_mode):
                raise HistorySourceError('历史来源内存在符号链接，没有沿链接读取或发布。')
            relative = prefix + '/' + name
            if stat.S_ISDIR(item.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    result.append((relative, _signature(os.fstat(child))))
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(item.st_mode) and name.endswith(('.jsonl', '.jsonl.zstd')):
                result.append((relative, _signature(item)))
    try:
        for branch in branches:
            try:
                item = os.stat(branch, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                result.append((branch, None))
                continue
            if stat.S_ISDIR(item.st_mode):
                child = os.open(branch, flags, dir_fd=root)
                try:
                    result.append((branch, _signature(os.fstat(child))))
                    visit(child, branch)
                finally:
                    os.close(child)
            elif stat.S_ISREG(item.st_mode):
                result.append((branch, _signature(item)))
            else:
                raise HistorySourceError('历史来源不是允许的普通目录或文件。')
        return tuple(result)
    finally:
        os.close(root)


def _digest_rows(connection, tables, mutable_meta=(), owned_agents=()):
    result = {}
    for name in tables:
        identifier = '"' + name.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute('PRAGMA table_info(' + identifier + ')')}
        where, parameters = '', ()
        if name == 'meta' and mutable_meta:
            where = ' WHERE key NOT IN (' + ','.join('?' for _ in mutable_meta) + ')'
            parameters = tuple(mutable_meta)
        elif name in ('sessions', 'messages', 'native_attachments', 'native_image_messages') and owned_agents:
            where = ' WHERE agent NOT IN (' + ','.join('?' for _ in owned_agents) + ')'
            parameters = tuple(owned_agents)
        try:
            query = 'SELECT rowid,* FROM ' + identifier + where + ' ORDER BY rowid'
            rows = connection.execute(query, parameters)
        except sqlite3.OperationalError:
            # WITHOUT ROWID tables still keep all original field values; their
            # primary key is already the physical identity preserved by backup.
            rows = connection.execute('SELECT * FROM ' + identifier + where + ' ORDER BY ' +
                                      ','.join('"' + col.replace('"', '""') + '"' for col in sorted(columns)), parameters)
        digest, count = hashlib.sha256(), 0
        for row in rows:
            values = [(type(value).__name__, value.hex() if isinstance(value, bytes) else value) for value in row]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode())
            digest.update(b'\n')
            count += 1
        result[name] = (count, digest.hexdigest())
    return result


class HistorySources:
    def __init__(self, index_path, *, indexer, codex_root, hermes_db, claude_root=None, pi_root=None,
                 dsh_root=None, dsh_decoder=None):
        self.indexer = indexer
        self.index_path = _path(index_path)
        if self.indexer.assert_local_index(self.index_path) != self.index_path:
            raise HistorySourceError('App历史索引路径不能经过符号链接。')
        self.codex_root, self.hermes_db = _path(codex_root), _path(hermes_db)
        self.claude_root = _path(claude_root) if claude_root is not None else None
        self.pi_root = _path(pi_root) if pi_root is not None else None
        if (dsh_root is None) != (dsh_decoder is None) or dsh_decoder is not None and not callable(dsh_decoder):
            raise HistorySourceError('DSH历史需要明确的存储根目录与只读解码器。')
        self.dsh_root = _path(dsh_root) if dsh_root is not None else None
        self.dsh_decoder = dsh_decoder
        # This publisher owns a derived index, never an original source.
        roots = (self.codex_root,) + tuple(root for root in (self.claude_root, self.pi_root, self.dsh_root) if root is not None)
        if (self.index_path == self.hermes_db or
                any(self.index_path == root or root in self.index_path.parents for root in roots)):
            raise HistorySourceError('App历史索引必须与原生历史来源分开。')
        try:
            same_hermes = os.path.samefile(self.index_path, self.hermes_db)
        except FileNotFoundError:
            same_hermes = False
        if same_hermes:
            raise HistorySourceError('App历史索引不能与原生历史库共用同一文件。')

    def _owned_sources(self):
        """Only explicitly supplied native roots contribute extra sources."""
        values = []
        if self.claude_root is not None:
            values.append(('claude', self.claude_root, ('projects',), ingest_claude, CLAUDE_META_KEY))
        if self.pi_root is not None:
            values.append(('pi', self.pi_root, ('sessions',), ingest_pi, PI_META_KEY))
        if self.dsh_root is not None:
            from dsh_history import ingest_dsh, META_KEY as DSH_META_KEY
            values.append(('dsh', self.dsh_root, ('.',), partial(ingest_dsh, decoder=self.dsh_decoder), DSH_META_KEY))
        return tuple(values)

    @staticmethod
    def _merge_delta(stats, agent, delta, *, copied=False, named_sections=True):
        for key, value in delta.items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if key.startswith(agent + '_'):
                stats[key] = value
            else:
                old = stats.get(key, 0)
                stats[key] = old + value if isinstance(old, int) and not isinstance(old, bool) else None
        coverage = stats.setdefault('coverage', {})
        for key, value in delta.get('coverage', {}).items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            old = coverage.get(key, None if copied else 0)
            coverage[key] = old + value if isinstance(old, int) and not isinstance(old, bool) else None
        if named_sections:
            stats[agent + '_coverage'] = dict(delta.get('coverage', {}))
        if isinstance(delta.get('native_images'), dict):
            if named_sections:
                stats[agent + '_native_images'] = dict(delta['native_images'])
            if not copied:
                images = stats.setdefault('native_images', {})
                for key, value in delta['native_images'].items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        images[key] = images.get(key, 0) + value

    def _callbacks(self, agents=None):
        callbacks = []
        for agent, root, _, ingest, _ in self._owned_sources():
            if agents is not None and agent not in agents:
                continue
            def callback(connection, stats, agent=agent, root=root, ingest=ingest):
                delta = {'source_errors': 0, 'coverage': {}}
                ingest(connection, root, delta, indexer=self.indexer)
                self._merge_delta(stats, agent, delta, named_sections=self.pi_root is not None or self.dsh_root is not None)
            callbacks.append(callback)
        return tuple(callbacks)

    def _sources(self, bootstrap=False, agents=None):
        values = []
        if not bootstrap:
            values.extend(((self.codex_root, True, ('sessions', 'archived_sessions', 'session_index.jsonl')),
                           (self.hermes_db, False, ())))
        values.extend((root, True, branches) for agent, root, branches, _, _ in self._owned_sources()
                      if agents is None or agent in agents)
        sources = tuple((path, tree, branches, _source_state(path, tree, branches))
                        for path, tree, branches in values)
        if self.index_path.exists():
            identity = self.index_path.stat(follow_symlinks=False)
            target_inode = (identity.st_dev, identity.st_ino)
            for _, tree, _, state in sources:
                if tree and state and any(signature is not None and signature[:2] == target_inode
                                          for _, signature in state):
                    raise HistorySourceError('App历史索引不能与原生历史文件共用同一文件。')
        return sources

    @staticmethod
    def _check_sources(sources):
        for path, tree, branches, before in sources:
            if before != _source_state(path, tree, branches):
                raise HistorySourceError('历史来源在准备期间变化，现有索引保留，请重新更新。')

    def _locked(self, operation):
        parent, lock = None, None
        try:
            parent = _directory(self.index_path.parent, create=True)
            lock = os.open(self.index_path.name + '.refresh.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                           mode=0o600, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise HistorySourceError('App历史发布锁必须是普通文件。')
            os.fchmod(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return operation(parent)
        except (OSError, sqlite3.Error):
            raise HistorySourceError('本机历史来源或私有发布目录暂时不可用；现有索引没有被覆盖。') from None
        finally:
            if lock is not None:
                os.close(lock)
            if parent is not None:
                os.close(parent)

    def _stage(self, parent):
        fd, value = tempfile.mkstemp(prefix='.app-history-', suffix='.sqlite', dir=self.index_path.parent)
        os.fchmod(fd, 0o600)
        stage_identity = _signature(os.fstat(fd))[:2]
        os.close(fd)
        path = Path(value)
        # Own the temporary builder's lock identity too; it may safely reuse
        # this regular file, and cleanup will never unlink an unrelated lock.
        lock_name = path.name + '.refresh.lock'
        try:
            lock = os.open(lock_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           mode=0o600, dir_fd=parent)
        except BaseException:
            existing_stage = _file_state(parent, path.name)
            if existing_stage is not None and existing_stage[:2] == stage_identity:
                os.unlink(path.name, dir_fd=parent)
            raise
        lock_identity = _signature(os.fstat(lock))[:2]
        os.close(lock)
        return path, lock_identity

    @staticmethod
    def _cleanup(parent, stage, lock_identity):
        for name in (stage.name, stage.name + '-journal', stage.name + '-wal', stage.name + '-shm'):
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
        lock_name = stage.name + '.refresh.lock'
        state = _file_state(parent, lock_name)
        if state is not None and state[:2] == lock_identity:
            os.unlink(lock_name, dir_fd=parent)

    def _publish(self, parent, stage, previous):
        descriptor = os.open(stage.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise HistorySourceError('私有历史暂存文件无效。')
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        current = _file_state(parent, self.index_path.name)
        if previous is None:
            if current is not None:
                return False
            try:
                os.link(stage.name, self.index_path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                return False
        else:
            if current != previous:
                raise HistorySourceError('App历史索引在更新期间被其他发布者修改，未覆盖新状态。')
            # The app is the unique publisher. This is not a general filesystem
            # compare-and-swap guarantee against arbitrary external writers.
            os.replace(stage.name, self.index_path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        return True

    def _refresh_locked(self, parent):
        previous = _file_state(parent, self.index_path.name)
        sources = self._sources()
        codex = _CodexSnapshot(self.codex_root, sources[0][3])
        stage, lock_identity = self._stage(parent)
        try:
            try:
                stats = self.indexer.refresh_index(stage, self.codex_root, self.hermes_db,
                                                   additional_sources=tuple(self._callbacks()), codex_snapshot=codex)
            except RuntimeError:
                raise HistorySourceError('来源未完整读取，现有App历史索引保留。') from None
            if stats.get('source_errors'):
                raise HistorySourceError('来源未完整读取，现有App历史索引保留。')
            # Non-Codex sources retain the original strict stability rule.
            # Codex may append, but every captured original prefix must still
            # match, including after the other sources have been read.
            codex.verify()
            self._check_sources(sources[1:])
            after = _source_state(*sources[0][:3])
            codex.verify_paths(sources[0][3], after)
            stats['source_pending'] = bool(codex.incomplete or after != sources[0][3])
            # A signature belongs only to a fresh scan, never to an
            # inherited index. It describes metadata, not transcript content.
            with sqlite3.connect(stage) as connection:
                connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                                   (_SOURCE_SIGNATURE_KEY, _source_signature(sources)))
                connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                                   (_SOURCE_INCOMPLETE_KEY, '1' if codex.incomplete else '0'))
                connection.execute("UPDATE meta SET value=? WHERE key='stats'", (json.dumps(stats, sort_keys=True),))
            if not self._publish(parent, stage, previous):
                raise HistorySourceError('App历史索引已经建立；此次新发布没有覆盖它。')
            return stats
        finally:
            self._cleanup(parent, stage, lock_identity)

    def refresh(self):
        return self._locked(self._refresh_locked)

    def source_changes(self):
        """Compare metadata with the last complete scan, without updating it.

        A changing/unreadable index or source is unknown. The result makes no
        claim about changes made after the observation or unindexed data types.
        """
        parent = descriptor = None
        try:
            parent = _directory(self.index_path.parent)
            before = _file_state(parent, self.index_path.name)
            if before is None:
                return 'unknown'
            for suffix in ('-wal', '-shm', '-journal'):
                sidecar = _file_state(parent, self.index_path.name + suffix)
                if sidecar is not None and sidecar[2]:
                    return 'unknown'
            descriptor = os.open(self.index_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if _signature(os.fstat(descriptor)) != before:
                return 'unknown'
            connection = sqlite3.connect('file:' + quote('/dev/fd/' + str(descriptor), safe='/') + '?mode=ro&immutable=1', uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                row = connection.execute('SELECT value FROM meta WHERE key=?', (_SOURCE_SIGNATURE_KEY,)).fetchone()
                incomplete = connection.execute('SELECT value FROM meta WHERE key=?', (_SOURCE_INCOMPLETE_KEY,)).fetchone()
            finally:
                connection.close()
            if not row or type(row[0]) is not str or len(row[0]) != 64 or any(c not in '0123456789abcdef' for c in row[0]):
                return 'unknown'
            sources = self._sources()
            self._check_sources(sources)
            if (_signature(os.fstat(descriptor)) != before or _file_state(parent, self.index_path.name) != before
                    or any((state := _file_state(parent, self.index_path.name + suffix)) is not None and state[2]
                           for suffix in ('-wal', '-shm', '-journal'))):
                return 'unknown'
            if _source_signature(sources) != row[0]:
                return 'changed'
            return 'unknown' if incomplete and incomplete[0] == '1' else 'unchanged'
        except (OSError, sqlite3.Error, HistorySourceError, ValueError, TypeError):
            return 'unknown'
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)

    def _bootstrap_stats(self, connection, deltas, *, extension=False):
        row = connection.execute("SELECT value FROM meta WHERE key='stats'").fetchone()
        try:
            inherited = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            raise HistorySourceError('旧历史库的统计格式无效，未发布副本。') from None
        if not isinstance(inherited, dict):
            raise HistorySourceError('旧历史库的统计格式无效，未发布副本。')
        stats = dict(inherited)
        old_coverage = inherited.get('coverage')
        old_coverage = old_coverage if isinstance(old_coverage, dict) else {}
        stats['coverage'] = dict(old_coverage)
        for agent, delta in deltas:
            self._merge_delta(stats, agent, delta, copied=True)
        names = '_and_'.join(agent for agent, _ in deltas)
        # Inherited scans remain explicitly inherited. New sources get their
        # own measured image/coverage fields, including honest unavailable slots.
        stats['coverage']['count_basis'] = ('app_snapshot_plus_' if extension else 'legacy_snapshot_plus_') + names + '_scan'
        stats['sessions'] = connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        stats['messages'] = connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        old_time = connection.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        stats['bootstrap'] = {'method': 'readonly_backup_plus_' + names, 'legacy_source_scan_repeated': False,
                              'legacy_built_at': self.indexer.timestamp(old_time[0]) if old_time else '',
                              'native_images_basis': 'inherited_legacy_snapshot'}
        if extension:
            stats['bootstrap']['method'] = 'readonly_app_backup_plus_' + names
            stats['bootstrap']['native_images_basis'] = 'inherited_app_snapshot'
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('stats', json.dumps(stats, sort_keys=True)))
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                           ('built_at', self.indexer.timestamp(dt.datetime.now(dt.timezone.utc).isoformat())))

    def _copy_from_locked(self, parent, source_path, *, previous, additions, extension=False):
        """One read-only snapshot path serves first boot and source extension."""
        try:
            source_parent = _directory(source_path.parent)
        except FileNotFoundError:
            if extension:
                raise HistorySourceError('App历史索引尚未建立，不能追加原生来源。') from None
            self._refresh_locked(parent)
            return True
        source = None
        try:
            if _file_state(source_parent, source_path.name) is None:
                if extension:
                    raise HistorySourceError('App历史索引尚未建立，不能追加原生来源。')
                self._refresh_locked(parent)
                return True
            source = os.open(source_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=source_parent)
            before = os.fstat(source)
            header = os.pread(source, 100, 0)
            if (not stat.S_ISREG(before.st_mode) or not header.startswith(b'SQLite format 3\x00') or
                    header[18:20] != b'\x01\x01'):
                raise HistorySourceError('来源索引必须是完整的普通DELETE日志SQLite库；WAL库没有被忽略日志复制。')
            sidecars = tuple((suffix, _file_state(source_parent, source_path.name + suffix))
                             for suffix in ('-journal', '-wal', '-shm'))
            if any(state is not None and state[2] for _, state in sidecars):
                raise HistorySourceError('来源索引仍有未确认的日志状态，没有复制或发布。')
            def guard():
                if (_signature(before) != _signature(os.fstat(source)) or
                        _file_state(source_parent, source_path.name) != _signature(before) or
                        any(_file_state(source_parent, source_path.name + suffix) != state
                            for suffix, state in sidecars)):
                    raise HistorySourceError('历史索引在复制期间变化，未发布该副本。')
            reader = sqlite3.connect('file:' + quote('/dev/fd/' + str(source), safe='/') +
                                     '?mode=ro&immutable=1', uri=True)
            try:
                stage, lock_identity = self._stage(parent)
            except BaseException:
                reader.close()
                raise
            destination = None
            try:
                reader.execute('PRAGMA query_only=ON')
                if reader.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise HistorySourceError('来源历史库完整性检查未通过。')
                tables = [row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                source_tables = ('sessions', 'messages', 'native_attachments', 'native_image_messages')
                if not {'meta', *source_tables}.issubset(tables):
                    raise HistorySourceError('来源历史库缺少必要表，没有发布不完整副本。')
                version = reader.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if not version or version[0] != str(self.indexer.SCHEMA_VERSION):
                    raise HistorySourceError('来源历史库格式版本暂不支持。')
                agents = tuple(item[0] for item in additions)
                forbidden = agents if extension else ('claude', 'pi', 'dsh')
                for agent in forbidden:
                    if any(reader.execute('SELECT COUNT(*) FROM ' + table + ' WHERE agent=?',
                                          (agent,)).fetchone()[0] for table in source_tables):
                        raise HistorySourceError('来源库已有待追加来源；不能以首次复制流程混合覆盖。')
                    meta_key = dict((item[0], item[4]) for item in self._owned_sources()).get(agent,
                        {'claude': CLAUDE_META_KEY, 'pi': PI_META_KEY, 'dsh': 'dsh_session_coverage'}[agent])
                    if reader.execute('SELECT 1 FROM meta WHERE key=?', (meta_key,)).fetchone():
                        raise HistorySourceError('来源库已有待追加来源的覆盖元数据，不能再次追加。')
                destination = sqlite3.connect(stage)
                reader.backup(destination)
                guard()
                schema = destination.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                mutable = ('built_at', 'stats') + tuple(item[4] for item in additions) if additions else ()
                originals = _digest_rows(destination, tables, mutable, agents)
                if additions:
                    sources = self._sources(bootstrap=True, agents=agents)
                    deltas = []
                    for agent, root, _, ingest, _ in additions:
                        delta = {'source_errors': 0, 'coverage': {}}
                        ingest(destination, root, delta, indexer=self.indexer)
                        if delta.get('source_errors'):
                            raise HistorySourceError('新增来源未完整读取，原索引保留，App副本未发布。')
                        deltas.append((agent, delta))
                    self._check_sources(sources)
                    destination.execute("""UPDATE sessions SET
                        message_count=(SELECT COUNT(*) FROM messages m WHERE m.agent=sessions.agent AND m.session_id=sessions.session_id),
                        updated_at=COALESCE((SELECT MAX(timestamp) FROM messages m WHERE m.agent=sessions.agent AND m.session_id=sessions.session_id),started_at)
                        WHERE agent IN (""" + ','.join('?' for _ in agents) + ')', agents)
                    self._bootstrap_stats(destination, deltas, extension=extension)
                if (originals != _digest_rows(destination, tables, mutable, agents) or
                        schema != destination.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()):
                    raise HistorySourceError('原历史记录或表结构核对不一致，未发布副本。')
                # A copied or extended index was not a complete source scan.
                destination.execute('DELETE FROM meta WHERE key=?', (_SOURCE_SIGNATURE_KEY,))
                destination.commit()
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise HistorySourceError('新历史库完整性检查未通过。')
                destination.close()
                destination = None
                guard()
                return self._publish(parent, stage, previous)
            finally:
                if destination is not None:
                    destination.close()
                reader.close()
                self._cleanup(parent, stage, lock_identity)
        finally:
            if source is not None:
                os.close(source)
            os.close(source_parent)

    def bootstrap_from(self, legacy_index):
        legacy = _path(legacy_index)
        def bootstrap(parent):
            if _file_state(parent, self.index_path.name) is not None:
                return False
            if legacy == self.index_path:
                raise HistorySourceError('旧索引与App索引必须是不同文件。')
            return self._copy_from_locked(parent, legacy, previous=None, additions=self._owned_sources())
        return self._locked(bootstrap)

    def extend_owned_sources(self, agents=('pi',)):
        """Add explicitly enabled, previously absent sources to an App snapshot.

        This is a first-add operation. A later update must use refresh(), which
        rebuilds each native branch in a fresh derived index rather than leaving
        obsolete messages under stable main-session identities.
        """
        available = {item[0]: item for item in self._owned_sources()}
        if (not isinstance(agents, (tuple, list)) or not agents or
                any(not isinstance(agent, str) or agent not in available for agent in agents) or
                len(set(agents)) != len(agents)):
            raise HistorySourceError('追加来源必须是明确启用且不重复的原生来源。')
        additions = tuple(available[agent] for agent in agents)
        def extend(parent):
            previous = _file_state(parent, self.index_path.name)
            if previous is None:
                raise HistorySourceError('App历史索引尚未建立，不能追加原生来源。')
            return self._copy_from_locked(parent, self.index_path, previous=previous,
                                          additions=additions, extension=True)
        return self._locked(extend)
