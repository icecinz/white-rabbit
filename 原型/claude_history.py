"""Bounded, read-only Claude Code text history; no CLI or model calls.

The current-leaf rule follows Anthropic's get_session_messages implementation:
https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/sessions.py
Older branches stay independently indexed; compaction logical parents are not
invented links. Callers inject their indexer, so this module has no import cycle.
"""
import hashlib
import json
import os
from pathlib import Path
import stat


META_KEY = 'claude_session_coverage'
ENTRY_TYPES = frozenset(('user', 'assistant', 'system', 'progress', 'attachment'))
IMAGE_TYPES = frozenset(('image', 'input_image', 'output_image', 'image_url', 'local_image'))
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_FILE_RECORDS = 200000
MAX_FILES = 10000


def _count(stats, key, amount=1):
    stats[key] = stats.get(key, 0) + amount


def _opaque(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _source_id(session_id, category, anchor):
    value = session_id + ':' + category + ':' + anchor
    if len(value) <= 256:
        return value
    return ('sha256_' + hashlib.sha256(session_id.encode()).hexdigest() + ':' + category + ':' +
            hashlib.sha256(anchor.encode()).hexdigest())


def _text(entry, coverage=None):
    message = entry.get('message')
    if not isinstance(message, dict) or entry.get('type') not in ('user', 'assistant'):
        return ''
    if message.get('role') != entry['type']:
        return ''
    content = message.get('content')
    if isinstance(content, str):
        # A JSON-looking user example remains the same string, never decoded.
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    if coverage is not None:
        _count(coverage, 'structured_messages')
    attachments = 0
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
            parts.append(block['text'])
        elif coverage is not None:
            _count(coverage, 'nontext_blocks_skipped')
            kind = block.get('type') if isinstance(block, dict) else None
            if kind in ('image', 'document', 'input_image', 'image_url'):
                attachments += 1
                _count(coverage, 'attachment_blocks_skipped')
            elif kind not in ('thinking', 'redacted_thinking', 'tool_use', 'tool_result'):
                _count(coverage, 'unsupported_structured_payloads')
    value = '\n'.join(parts)
    if coverage is not None and attachments and not value.strip():
        _count(coverage, 'attachment_only_messages_skipped')
    return value


def _eligible(entry):
    message = entry.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    image = (isinstance(message, dict) and message.get('role') == entry.get('type') and
             isinstance(content, list) and any(isinstance(block, dict) and isinstance(block.get('type'), str) and block['type'] in IMAGE_TYPES
                                               for block in content))
    return (entry.get('type') in ('user', 'assistant') and
            (not entry.get('isMeta') or entry.get('isCompactSummary') is True) and (bool(_text(entry).strip()) or image))


def _image_placeholders(entry, indexer):
    """Fingerprint source values without decoding, fetching or copying pixels.

    All source image forms are described as unsupported native 'image' blocks,
    whose existing decoder immediately returns unavailable. Original positions
    are retained; payload values are only hashed by the bounded helper.
    """
    message = entry.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, list):
        return {'attachments': [], 'image_blocks': 0, 'omitted_image_blocks': 0, 'invalid_payloads': 0}
    safe = [None] * len(content)
    for position, block in enumerate(content):
        if not isinstance(block, dict) or not isinstance(block.get('type'), str) or block['type'] not in IMAGE_TYPES:
            continue
        image = indexer._native_image_value(block)
        safe[position] = {'type': 'image', 'image_url': image[1] if image else None}
    if not any(block is not None for block in safe):
        return {'attachments': [], 'image_blocks': 0, 'omitted_image_blocks': 0, 'invalid_payloads': 0}
    described = indexer.describe_native_attachments(safe)
    for item in described['attachments']:
        if item['kind'] != 'inspection_summary':
            item.update(state='unavailable', reason='claude_image_not_integrated', size_bytes=0, sha256='')
    return described


def _native_stats(stats):
    counts = stats.setdefault('native_images', {})
    for key in ('image_blocks_scanned', 'embedded_blocks_scanned', 'unavailable_descriptors_scanned',
                'image_blocks_omitted', 'messages_with_unknown_omissions', 'invalid_payloads', 'native_id_conflicts'):
        counts.setdefault(key, 0)


def _walk(leaf, nodes):
    seen, chain, missing, cycle = set(), [], 0, False
    while leaf is not None:
        uid = leaf['uuid']
        if uid in seen:
            cycle = True
            break
        seen.add(uid)
        chain.append(leaf)
        parent = leaf.get('parentUuid')
        if parent is None:
            break
        if not _opaque(parent) or parent not in nodes:
            missing += 1
            break
        leaf = nodes[parent]
    chain.reverse()
    return chain, missing, cycle


def _current_leaf(nodes, child):
    parents = {entry.get('parentUuid') for entry in nodes.values() if isinstance(entry.get('parentUuid'), str)}
    leaves = {}
    for terminal in nodes.values():
        if terminal['uuid'] in parents:
            continue
        seen, cursor = set(), terminal
        while cursor is not None and cursor['uuid'] not in seen:
            seen.add(cursor['uuid'])
            if cursor.get('type') in ('user', 'assistant'):
                leaves[cursor['uuid']] = cursor
                break
            cursor = nodes.get(cursor.get('parentUuid'))
    preferred = [entry for entry in leaves.values() if not entry.get('isMeta') and
                 (child or not entry.get('isSidechain') and not entry.get('teamName'))]
    candidates = preferred or list(leaves.values())
    return max(candidates, key=lambda entry: entry['_line']) if candidates else None


def _branch_counts(chain):
    values = {'summary_count': 0, 'omitted_image_blocks': 0, 'omitted_tool_blocks': 0,
              'omitted_thinking_blocks': 0, 'omitted_meta_messages': 0, 'invalid_message_records': 0}
    for entry in chain:
        if entry.get('isCompactSummary') is True and _text(entry).strip():
            values['summary_count'] += 1
        elif entry.get('isMeta'):
            values['omitted_meta_messages'] += 1
        message = entry.get('message')
        if entry.get('type') in ('user', 'assistant') and (not isinstance(message, dict) or
                message.get('role') != entry['type'] or not isinstance(message.get('content'), (str, list))):
            values['invalid_message_records'] += 1
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block.get('type') if isinstance(block, dict) else None
            if kind in ('image', 'document', 'input_image', 'image_url'):
                values['omitted_image_blocks'] += 1
            elif kind in ('tool_use', 'tool_result'):
                values['omitted_tool_blocks'] += 1
            elif kind in ('thinking', 'redacted_thinking'):
                values['omitted_thinking_blocks'] += 1
    return values


def _title(records, session_id):
    # Only top-level title metadata is considered, never a tool input field.
    for field in ('customTitle', 'aiTitle', 'summary', 'lastPrompt'):
        for entry in reversed(records):
            if entry.get('type') not in ('custom-title', 'ai-title', 'summary', 'last-prompt') or isinstance(entry.get('message'), dict):
                continue
            if entry.get('sessionId') not in (None, session_id):
                continue
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                return value
    return ''


def _save_branch(connection, source_id, source_session, leaf, chain, integrity, title,
                 path, child, archive, stats, indexer, metadata):
    rows = [entry for entry in chain if _eligible(entry)]
    started = next((indexer.timestamp(entry.get('timestamp')) for entry in chain
                    if indexer.timestamp(entry.get('timestamp'))), '')
    indexer.save_session(connection, 'claude', source_id, title=title,
                         cwd=next((entry.get('cwd') for entry in chain if isinstance(entry.get('cwd'), str)), ''),
                         parent=source_session if child else None,
                         forked=source_session if archive else None, archived=archive,
                         started=started, source_path=path)
    for ordinal, entry in enumerate(rows, 1):
        phase = 'compact_summary' if entry.get('isCompactSummary') is True else ''
        text, when = _text(entry), indexer.timestamp(entry.get('timestamp'))
        duplicate = indexer._native_message_exists(connection, 'claude', source_id, entry['uuid'])
        if text.strip():
            wrote = indexer.save_message(connection, 'claude', source_id, entry['uuid'], entry['type'],
                                         when, text, path, entry['_line'], ordinal, phase)
            _count(stats, 'claude_messages_added', wrote)
            _count(stats, 'duplicates_skipped', 1 - wrote)
            if phase:
                _count(stats, 'claude_summaries_added', wrote)
        described = _image_placeholders(entry, indexer)
        if described['attachments']:
            _native_stats(stats)
            original = {key: value for key, value in entry.items() if key not in ('_line', '_record_sha256')}
            locator = {'kind': 'claude_jsonl', 'source_path': str(path), 'source_line': entry['_line'],
                       'record_sha256': entry.get('_record_sha256') or hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            indexer.save_native_attachments(connection, 'claude', source_id, entry['uuid'], entry['type'], when,
                                            text, path, entry['_line'], ordinal, phase, described,
                                            {'content': locator}, duplicate, stats)
            if not text.strip() and not duplicate:
                _count(stats, 'claude_image_only_messages_added')
    record = dict(integrity, **_branch_counts(chain), branch_kind='child' if child else 'archive' if archive else 'main',
                  source_session_id=source_session, leaf_uuid=leaf['uuid'] if leaf else '',
                  text_messages=sum(bool(_text(entry).strip()) for entry in rows),
                  image_only_messages=sum(not _text(entry).strip() for entry in rows))
    record['continuable'] = bool(rows) and not any((record['missing_parents'], record['cycle'], record['uuid_conflict'],
                                                  record['unparsed_lines'], record['unsupported_compaction'], record['invalid_message_records'],
                                                  record['ambiguous_copies']))
    metadata[source_id] = record
    _count(stats, 'claude_sessions_added')
    if archive:
        _count(stats, 'claude_branches_added')


def _ingest_records(connection, records, path, owner, file_agent, file_bad_lines, stats, indexer, metadata, seen_sources):
    groups = {}
    for entry in records:
        if entry.get('type') not in ENTRY_TYPES:
            continue
        session = entry.get('sessionId')
        if not _opaque(session):
            session = owner
        if not _opaque(session):
            _count(stats, 'claude_unidentified_records')
            continue
        child = entry.get('isSidechain') is True or file_agent is not None
        agent = entry.get('agentId') if child else None
        if child and not _opaque(agent):
            agent = file_agent or 'unidentified'
        groups.setdefault((session, agent), []).append(entry)
    for (session, agent), entries in groups.items():
        child = agent is not None
        source = _source_id(session, 'agent', agent) if child else session
        original_source = source
        copied_source = source in seen_sources and seen_sources[source] != str(path)
        seen_sources.setdefault(source, str(path))
        if copied_source:
            # Two physical transcripts with one session identity must never
            # merge under INSERT OR IGNORE. Keep the second chain separately
            # and make the ambiguous default require an explicit material pick.
            previous = metadata.get(source)
            if previous:
                previous['continuable'] = False
                previous['ambiguous_copies'] = previous.get('ambiguous_copies', 0) + 1
            anchor = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            _count(stats, 'claude_ambiguous_source_copies')
        nodes, conflicts, unlinked = {}, [], []
        unsupported = 0
        for entry in entries:
            compact = entry.get('compactMetadata') or entry.get('compact_metadata')
            if entry.get('subtype') == 'compact_boundary' and isinstance(compact, dict):
                unsupported += int('preserved_messages' in compact or 'preservedMessages' in compact)
            uid = entry.get('uuid')
            if not _opaque(uid):
                if _eligible(entry):
                    candidate = dict(entry)
                    candidate['uuid'] = 'sha256:' + hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    unlinked.append(candidate)
                continue
            if uid in nodes:
                previous = {key: value for key, value in nodes[uid].items() if key not in ('_line', '_record_sha256')}
                current = {key: value for key, value in entry.items() if key not in ('_line', '_record_sha256')}
                if previous == current:
                    _count(stats, 'claude_duplicate_records')
                else:
                    conflicts.append(entry)
                    _count(stats, 'claude_uuid_conflicts')
                continue
            nodes[uid] = entry
        integrity = {'missing_parents': 0, 'cycle': False, 'uuid_conflict': len(conflicts),
                     'unparsed_lines': file_bad_lines + len(unlinked), 'unsupported_compaction': unsupported,
                     'ambiguous_copies': int(copied_source)}
        base_title = _title(records, session)
        title = ('Claude 子代理 · ' + base_title if child and base_title else 'Claude 子代理' if child else base_title)
        leaf = _current_leaf(nodes, child)
        if leaf is None and nodes:
            # A cycle has no terminal: preserve its readable records as an
            # explicitly non-continuable source instead of claiming emptiness.
            leaf = max(nodes.values(), key=lambda entry: entry['_line'])
        if copied_source:
            source = _source_id(original_source, 'branch', (leaf['uuid'] if leaf else 'unlinked') + ':copy-' + anchor)
        chain, missing, cycle = _walk(leaf, nodes) if leaf else ([], 0, False)
        first_integrity = dict(integrity, missing_parents=missing, cycle=cycle)
        _save_branch(connection, source, session, leaf, chain, first_integrity,
                     'Claude 同ID未核验副本 · ' + title if copied_source else title, path,
                     child, copied_source, stats, indexer, metadata)
        covered = {entry['uuid'] for entry in chain if _eligible(entry)}
        candidates = sorted((entry for entry in nodes.values() if _eligible(entry)), key=lambda entry: entry['_line'], reverse=True)
        for old_leaf in candidates:
            if old_leaf['uuid'] in covered:
                continue
            old_chain, old_missing, old_cycle = _walk(old_leaf, nodes)
            branch = _source_id(source, 'branch', old_leaf['uuid'])
            _save_branch(connection, branch, session, old_leaf, old_chain,
                         dict(integrity, missing_parents=old_missing, cycle=old_cycle),
                         ('Claude 旧分支 · ' + base_title).rstrip(' ·'), path,
                         child, True, stats, indexer, metadata)
            covered.update(entry['uuid'] for entry in old_chain if _eligible(entry))
        for alternative in conflicts + unlinked:
            if not _eligible(alternative):
                continue
            own_nodes = dict(nodes, **{alternative['uuid']: alternative})
            old_chain, old_missing, old_cycle = _walk(alternative, own_nodes)
            signature = hashlib.sha256(json.dumps(alternative, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            branch = _source_id(source, 'branch', alternative['uuid'] + ':unverified:' + signature)
            _save_branch(connection, branch, session, alternative, old_chain,
                         dict(integrity, missing_parents=old_missing, cycle=old_cycle,
                              uuid_conflict=max(1, len(conflicts)) if alternative in conflicts else len(conflicts),
                              unparsed_lines=max(1, integrity['unparsed_lines'])),
                         'Claude 未核验原文 · ' + base_title, path, child, True, stats, indexer, metadata)


def _open_root(root):
    # Every directory component is no-follow, not only the final '.claude'.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open('/', flags)
    try:
        for part in root.parts[1:]:
            nxt = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = nxt
        return current
    except BaseException:
        os.close(current)
        raise


def _entries(fd, stats):
    for name in sorted(os.listdir(fd)):
        if name.startswith('.'):
            continue
        item = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(item.st_mode):
            _count(stats, 'claude_symlinks_skipped')
            continue
        yield name, item


def _scan_files(root, stats):
    """Yield only projects/<project>/*.jsonl and its fixed subagents shape."""
    handles = []
    try:
        fd = _open_root(root)
        handles.append(fd)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            projects = os.open('projects', flags, dir_fd=fd)
        except FileNotFoundError:
            return
        handles.append(projects)
        for project, item in _entries(projects, stats):
            if not stat.S_ISDIR(item.st_mode):
                continue
            project_fd = os.open(project, flags, dir_fd=projects)
            try:
                for name, metadata in _entries(project_fd, stats):
                    if stat.S_ISREG(metadata.st_mode) and name.endswith('.jsonl'):
                        yield project_fd, name, root / 'projects' / project / name, name[:-6], None
                    elif stat.S_ISDIR(metadata.st_mode):
                        session_fd = os.open(name, flags, dir_fd=project_fd)
                        try:
                            try:
                                agents_fd = os.open('subagents', flags, dir_fd=session_fd)
                            except FileNotFoundError:
                                continue
                            try:
                                for agent_file, agent_metadata in _entries(agents_fd, stats):
                                    if stat.S_ISREG(agent_metadata.st_mode) and agent_file.endswith('.jsonl'):
                                        agent_id = agent_file[:-6]
                                        if agent_id.startswith('agent-'):
                                            agent_id = agent_id[6:]
                                        yield agents_fd, agent_file, root / 'projects' / project / name / 'subagents' / agent_file, name, agent_id
                            finally:
                                os.close(agents_fd)
                        finally:
                            os.close(session_fd)
            finally:
                os.close(project_fd)
    finally:
        for fd in reversed(handles):
            os.close(fd)


def ingest_claude(connection, claude_root, stats, *, indexer):
    """Write only the caller's derived index. Native files are opened read-only.

    Per-source coverage is published into meta, including archives. The parent
    refresh owns transactions/atomic replacement and must refuse source_errors.
    """
    root = Path(os.path.abspath(Path(claude_root).expanduser()))
    metadata, seen_sources = {}, {}
    try:
        existing = connection.execute('SELECT value FROM meta WHERE key=?', (META_KEY,)).fetchone()
        if existing:
            parsed = json.loads(existing[0])
            if isinstance(parsed, dict):
                metadata.update(parsed)
        if not root.exists():
            connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (META_KEY, '{}'))
            return
        for number, (directory, name, path, owner, agent) in enumerate(_scan_files(root, stats), 1):
            if number > MAX_FILES:
                raise OSError('Claude source file limit exceeded')
            _count(stats, 'claude_files')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                    raise OSError('Claude source file size/type invalid')
                records, bad, line_number, read_bytes = [], 0, 0, 0
                with os.fdopen(fd, 'rb', closefd=False) as handle:
                    while True:
                        raw = handle.readline(indexer.MAX_SOURCE_LINE_BYTES + 1)
                        if not raw:
                            break
                        line_number += 1
                        read_bytes += len(raw)
                        if line_number > MAX_FILE_RECORDS or read_bytes > MAX_FILE_BYTES:
                            raise OSError('Claude source inspection limit exceeded')
                        if len(raw) > indexer.MAX_SOURCE_LINE_BYTES:
                            while raw and not raw.endswith(b'\n'):
                                raw = handle.readline(indexer.MAX_SOURCE_LINE_BYTES + 1)
                                read_bytes += len(raw)
                                if read_bytes > MAX_FILE_BYTES:
                                    raise OSError('Claude source inspection limit exceeded')
                            bad += 1
                            _count(stats, 'skipped_large_lines')
                            continue
                        try:
                            entry = json.loads(raw)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            if raw.strip():
                                bad += 1
                                _count(stats, 'skipped_invalid_lines')
                            continue
                        if not isinstance(entry, dict):
                            bad += 1
                            _count(stats, 'skipped_invalid_lines')
                            continue
                        entry['_line'] = line_number
                        entry['_record_sha256'] = hashlib.sha256(raw).hexdigest()
                        records.append(entry)
                        _text(entry, stats.setdefault('coverage', {}))
                after = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise OSError('Claude source changed during inspection')
                _ingest_records(connection, records, path, owner, agent, bad, stats, indexer, metadata, seen_sources)
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError, RecursionError):
        _count(stats, 'source_errors')
    connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       (META_KEY, json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':'))))
