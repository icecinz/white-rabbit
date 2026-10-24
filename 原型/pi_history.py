"""Bounded read-only Pi v3 history, independently of Pi's mutable manager.

Tree/compaction semantics follow Pi 0.79.1 buildSessionContext/convertToLlm.
Only the caller's derived index is written. Generated context is represented by
the public indexer's user-runtime role plus an explicit phase/native-role marker;
it is never advertised as literal human speech or a privileged system message.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from rabbit_content.history_records import (PI_COMPACT_PREFIX as COMP_PREFIX, PI_COMPACT_SUFFIX as COMP_SUFFIX,
                             PI_BRANCH_PREFIX as BRANCH_PREFIX, PI_BRANCH_SUFFIX as BRANCH_SUFFIX,
                             PI_COVERAGE_MAX_BYTES, PI_CONTEXT_MAX_IDS)


META_KEY = 'pi_session_coverage'
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_FILE_RECORDS = 200000
MAX_FILES = 10000
MAX_SCAN_ENTRIES = 100000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_TOTAL_RECORDS = 500000
MAX_BRANCHES = 10000
MAX_CHAIN_STEPS = 2000000
MAX_PROJECTED_MESSAGES = 500000
MAX_PROJECTED_BYTES = 256 * 1024 * 1024
ENTRY_TYPES = frozenset(('message', 'compaction', 'branch_summary', 'custom_message',
                        'thinking_level_change', 'model_change', 'custom', 'label', 'session_info'))
IMAGE_TYPES = frozenset(('image',))


def _count(values, key, amount=1):
    values[key] = values.get(key, 0) + amount


def _string(value):
    return isinstance(value, str) and not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _opaque(value):
    return _string(value) and 0 < len(value) <= 256 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _source_id(session, kind, anchor):
    value = session + ':' + kind + ':' + anchor
    if len(value) <= 256:
        return value
    return 'sha256_' + hashlib.sha256(session.encode()).hexdigest() + ':' + kind + ':' + hashlib.sha256(anchor.encode()).hexdigest()


def _signature(item):
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_directory(fd, path, before):
    check = _directory(path)
    try:
        if _signature(os.fstat(fd)) != before or _signature(os.fstat(check)) != before:
            raise OSError('Pi source directory changed during inspection')
    finally:
        os.close(check)


def _names(fd, scanned):
    names = []
    with os.scandir(fd) as entries:
        for item in entries:
            scanned[0] += 1
            if scanned[0] > MAX_SCAN_ENTRIES:
                raise OSError('Pi source directory entry limit exceeded')
            names.append(item.name)
    return sorted(names)


def _files(root):
    """Only agent-root/sessions/*.jsonl and one encoded-cwd directory level."""
    try:
        root_fd = _directory(root)
    except FileNotFoundError:
        return
    root_before = _signature(os.fstat(root_fd))
    sessions = None
    scanned = [0]
    try:
        try:
            sessions = os.open('sessions', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            _check_directory(root_fd, root, root_before)
            return
        directory = root / 'sessions'
        sessions_before = _signature(os.fstat(sessions))
        for name in _names(sessions, scanned):
            if name.startswith('.'):
                continue
            item = os.stat(name, dir_fd=sessions, follow_symlinks=False)
            if stat.S_ISLNK(item.st_mode):
                raise OSError('Pi source symlink refused')
            if name.endswith('.jsonl') and not stat.S_ISDIR(item.st_mode):
                yield sessions, name, directory / name
            elif stat.S_ISDIR(item.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=sessions)
                child_path = directory / name
                child_before = _signature(os.fstat(child))
                try:
                    for filename in _names(child, scanned):
                        if filename.startswith('.'):
                            continue
                        metadata = os.stat(filename, dir_fd=child, follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode):
                            raise OSError('Pi source symlink refused')
                        if filename.endswith('.jsonl') and not stat.S_ISDIR(metadata.st_mode):
                            yield child, filename, child_path / filename
                    _check_directory(child, child_path, child_before)
                finally:
                    os.close(child)
        _check_directory(sessions, directory, sessions_before)
        _check_directory(root_fd, root, root_before)
    finally:
        if sessions is not None:
            os.close(sessions)
        os.close(root_fd)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON member')
        value[key] = item
    return value


def _constant(_value):
    raise ValueError('Non-JSON constant')


def _scan_coverage(entry, stats):
    """Generic raw-source counters are never multiplied by derived branches."""
    coverage = stats.setdefault('coverage', {})
    for key in ('structured_messages', 'nontext_blocks_skipped', 'attachment_blocks_skipped',
                'attachment_only_messages_skipped', 'invalid_structured_payloads', 'unsupported_structured_payloads'):
        coverage.setdefault(key, 0)
    if entry.get('type') == 'message':
        message = entry.get('message')
        if not isinstance(message, dict):
            _count(coverage, 'invalid_structured_payloads')
            return
        content = message.get('content')
        if message.get('role') == 'bashExecution':
            return
    elif entry.get('type') == 'custom_message':
        content = entry.get('content')
    else:
        return
    if _string(content):
        return
    if not isinstance(content, list):
        _count(coverage, 'invalid_structured_payloads')
        return
    _count(coverage, 'structured_messages')
    text, counts, _images = _content(content)
    _count(coverage, 'nontext_blocks_skipped', sum(not (isinstance(block, dict) and block.get('type') == 'text' and _string(block.get('text'))) for block in content))
    _count(coverage, 'attachment_blocks_skipped', counts['omitted_image_blocks'])
    if counts['omitted_image_blocks'] and not text.strip():
        _count(coverage, 'attachment_only_messages_skipped')
    if counts['unsupported_content_blocks']:
        _count(coverage, 'unsupported_structured_payloads')


def _read_file(directory, name, path, stats, budget):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise OSError('Pi source file size/type refused')
        before_signature = _signature(before)
        if _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before_signature:
            raise OSError('Pi source identity changed')
        records, bad, size, digest = [], 0, 0, hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            line = 0
            while True:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                line += 1
                size += len(raw)
                budget['bytes'] += len(raw)
                budget['records'] += 1
                if (len(raw) > MAX_LINE_BYTES or size > MAX_FILE_BYTES or line > MAX_FILE_RECORDS or
                        budget['bytes'] > MAX_TOTAL_BYTES or budget['records'] > MAX_TOTAL_RECORDS):
                    raise OSError('Pi source inspection limit exceeded')
                digest.update(raw)
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
                    if not isinstance(entry, dict):
                        raise ValueError('Pi source entry is not an object')
                except (ValueError, UnicodeDecodeError, RecursionError):
                    bad += 1
                    _count(stats, 'skipped_invalid_lines')
                    continue
                entry['_line'] = line
                entry['_record_sha256'] = hashlib.sha256(raw).hexdigest()
                records.append(entry)
                _scan_coverage(entry, stats)
        if _signature(os.fstat(fd)) != before_signature:
            raise OSError('Pi source changed during inspection')
        parent = _directory(path.parent)
        try:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _signature(current) != before_signature or _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before_signature:
                raise OSError('Pi source replaced during inspection')
        finally:
            os.close(parent)
        return records, bad, digest.hexdigest(), before_signature
    finally:
        os.close(fd)


def _content(content):
    """Pi strings remain strings, including JSON-looking human examples."""
    counts = {'omitted_image_blocks': 0, 'omitted_tool_blocks': 0,
              'omitted_thinking_blocks': 0, 'unsupported_content_blocks': 0}
    if _string(content):
        return content, counts, []
    if not isinstance(content, list):
        counts['unsupported_content_blocks'] = 1
        return '', counts, []
    parts, images = [], []
    for position, block in enumerate(content):
        kind = block.get('type') if isinstance(block, dict) else None
        if kind == 'text' and _string(block.get('text')):
            parts.append(block['text'])
        elif kind == 'image':
            images.append((position, block))
            counts['omitted_image_blocks'] += 1
            if not _string(block.get('data')) or not _opaque(block.get('mimeType')):
                counts['unsupported_content_blocks'] += 1
        elif kind == 'thinking' and _string(block.get('thinking')):
            counts['omitted_thinking_blocks'] += 1
        elif kind == 'toolCall' and _opaque(block.get('id')) and _string(block.get('name')) and isinstance(block.get('arguments'), dict):
            counts['omitted_tool_blocks'] += 1
        else:
            counts['unsupported_content_blocks'] += 1
    return '\n'.join(parts), counts, images


def _bash(message):
    # Mirrors messages.js/bashExecutionToText; nothing is executed or opened.
    if (not _string(message.get('command')) or not _string(message.get('output')) or
            any(key in message and type(message[key]) is not bool for key in ('cancelled', 'truncated', 'excludeFromContext')) or
            (message.get('exitCode') is not None and type(message.get('exitCode')) is not int) or
            (message.get('fullOutputPath') is not None and not _string(message.get('fullOutputPath')))):
        return None
    text = 'Ran `' + message['command'] + '`\n'
    text += '```\n' + message['output'] + '\n```' if message['output'] else '(no output)'
    if message.get('cancelled'):
        text += '\n\n(command cancelled)'
    elif message.get('exitCode') not in (None, 0):
        text += '\n\nCommand exited with code ' + str(message['exitCode'])
    if message.get('truncated') and message.get('fullOutputPath'):
        text += '\n\n[Output truncated. Full output: ' + message['fullOutputPath'] + ']'
    return text


def _project(entry):
    """One visible projection, its origin, exclusions and counted omissions."""
    kind, role, phase, content = entry.get('type'), 'user', '', None
    counts = {'omitted_image_blocks': 0, 'omitted_tool_blocks': 0, 'omitted_thinking_blocks': 0,
              'unsupported_content_blocks': 0, 'invalid_message_records': 0,
              'unknown_context_records': 0, 'omitted_metadata_records': 0,
              'omitted_tool_messages': 0, 'omitted_excluded_context_messages': 0,
              'omitted_failed_assistant_messages': 0}
    transform = {'native_role': kind, 'context_role': 'user', 'phase': ''}
    excluded = False
    if kind == 'message':
        message = entry.get('message')
        if not isinstance(message, dict):
            counts['invalid_message_records'] += 1
            return None, counts
        native_role = message.get('role')
        transform['native_role'] = native_role if _opaque(native_role) else 'unknown'
        if native_role in ('user', 'assistant'):
            role, content = native_role, message.get('content')
            transform['context_role'] = role
            if role == 'assistant':
                reason = message.get('stopReason')
                if reason in ('error', 'aborted'):
                    excluded, phase = True, 'incomplete_reply'
                    counts['omitted_failed_assistant_messages'] += 1
                elif 'stopReason' in message and reason not in ('stop', 'length', 'toolUse'):
                    excluded, phase = True, 'unverified_context'
                    counts['invalid_message_records'] += 1
        elif native_role == 'toolResult':
            counts['omitted_tool_messages'] += 1
            # Tool results are never shown as literal conversational prose.
            _text, more, _images = _content(message.get('content'))
            for key, amount in more.items():
                counts[key] += amount
            counts['omitted_tool_blocks'] += 1
            return None, counts
        elif native_role == 'custom':
            content, phase = message.get('content'), 'custom_context'
            transform.update(custom_type=message.get('customType') if _string(message.get('customType')) else '',
                             display=message.get('display') if type(message.get('display')) is bool else None)
            if not _opaque(message.get('customType')) or type(message.get('display')) is not bool:
                counts['invalid_message_records'] += 1
        elif native_role == 'bashExecution':
            content, phase = _bash(message), 'bash_context'
            excluded = message.get('excludeFromContext') is True
            if content is None:
                counts['invalid_message_records'] += 1
            if excluded:
                counts['omitted_excluded_context_messages'] += 1
            transform['exclude_from_context'] = excluded
        elif native_role in ('branchSummary', 'compactionSummary'):
            # Pi's public appendMessage forbids these; retain readable material
            # but do not silently invent a valid top-level tree compaction.
            content = message.get('summary')
            phase = 'branch_summary' if native_role == 'branchSummary' else 'compact_summary'
            counts['invalid_message_records'] += 1
        else:
            counts['unknown_context_records'] += 1
            content, phase, excluded = message.get('content'), 'unverified_context', True
    elif kind in ('compaction', 'branch_summary'):
        content = entry.get('summary')
        phase = 'compact_summary' if kind == 'compaction' else 'branch_summary'
        transform['native_role'] = 'compactionSummary' if kind == 'compaction' else 'branchSummary'
        if not _string(content):
            counts['invalid_message_records'] += 1
            content = ''
    elif kind == 'custom_message':
        content, phase = entry.get('content'), 'custom_context'
        transform.update(native_role='custom', custom_type=entry.get('customType') if _string(entry.get('customType')) else '',
                         display=entry.get('display') if type(entry.get('display')) is bool else None)
        if not _opaque(entry.get('customType')) or type(entry.get('display')) is not bool:
            counts['invalid_message_records'] += 1
    elif kind in ENTRY_TYPES:
        counts['omitted_metadata_records'] += 1
        return None, counts
    else:
        counts['unknown_context_records'] += 1
        # Visible text in a future record is still browsable, never silently
        # granted context semantics by the parser.
        content = entry.get('content', entry.get('summary'))
        phase, excluded = 'unverified_context', True
    text, more, images = _content(content)
    for key, amount in more.items():
        counts[key] += amount
    transform['phase'] = phase
    return {'entry': entry, 'role': role, 'phase': phase, 'text': text, 'images': images,
            'content_length': len(content) if isinstance(content, list) else 0,
            'excluded': excluded, 'transform': transform}, counts


def _walk(leaf, nodes, budget):
    chain, seen, missing, cycle = [], set(), 0, False
    current = leaf
    while current is not None:
        budget['steps'] += 1
        if budget['steps'] > MAX_CHAIN_STEPS:
            raise OSError('Pi branch inspection limit exceeded')
        uid = current['id']
        if uid in seen:
            cycle = True
            break
        seen.add(uid)
        chain.append(current)
        parent = current.get('parentId')
        if parent is None:
            break
        if not _opaque(parent) or parent not in nodes:
            missing += 1
            break
        current = nodes[parent]
    chain.reverse()
    return chain, missing, cycle


def _context(chain):
    """Exact effective entry order; archive order remains the uncut path."""
    latest = next((index for index in range(len(chain) - 1, -1, -1) if chain[index].get('type') == 'compaction'), None)
    if latest is None:
        return chain, 0
    compaction = chain[latest]
    first = compaction.get('firstKeptEntryId')
    kept = next((index for index in range(latest) if chain[index]['id'] == first), None)
    # A missing boundary means the official implementation silently discards
    # the kept range. Retain the summary/archive but explicitly fail closed.
    return [compaction] + (chain[kept:latest] if kept is not None else []) + chain[latest + 1:], int(kept is None or not _opaque(first))


def _images(row, indexer):
    if not row['images']:
        return {'attachments': [], 'image_blocks': 0, 'omitted_image_blocks': 0, 'invalid_payloads': 0}
    safe = [None] * row['content_length']
    for position, block in row['images']:
        # Public descriptor receives a hashed opaque marker in an unsupported
        # image shape. No base64 decoder, URL fetch or source-path read occurs.
        marker = hashlib.sha256(json.dumps(block, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        safe[position] = {'type': 'image', 'image_url': 'pi-sha256:' + marker}
    described = indexer.describe_native_attachments(safe)
    for item in described['attachments']:
        if item['kind'] != 'inspection_summary':
            item.update(state='unavailable', reason='pi_image_not_integrated', size_bytes=0, sha256='')
    return described


def _native_stats(stats):
    counts = stats.setdefault('native_images', {})
    for key in ('image_blocks_scanned', 'embedded_blocks_scanned', 'unavailable_descriptors_scanned',
                'image_blocks_omitted', 'messages_with_unknown_omissions', 'invalid_payloads', 'native_id_conflicts'):
        counts.setdefault(key, 0)


def _save_branch(connection, source, original, header, leaf, chain, integrity, archive,
                 path, stats, indexer, metadata, budget):
    budget['branches'] += 1
    if budget['branches'] > MAX_BRANCHES:
        raise OSError('Pi source branch limit exceeded')
    file_id = hashlib.sha256(str(path).encode()).hexdigest()
    if source in metadata:
        # Opaque fixture/export IDs can resemble our branch namespace. Never
        # let one physical transcript overwrite another derived identity.
        previous = metadata[source]
        previous['ambiguous_copies'] += 1
        previous['continuable'] = False
        integrity = dict(integrity, ambiguous_copies=integrity['ambiguous_copies'] + 1)
        anchor = file_id[:24] + ':' + (leaf['id'] if leaf else 'empty')
        source = _source_id(source, 'source', anchor)
        if source in metadata:
            raise OSError('Pi derived identity collision')
        _count(stats, 'pi_source_id_collisions')
    projected, counts = {}, {}
    for entry in chain:
        row, more = _project(entry)
        for key, amount in more.items():
            _count(counts, key, amount)
        if row and (row['text'].strip() or row['images'] or row['phase'] == 'compact_summary'):
            projected[entry['id']] = row
    title = next((entry.get('name') for entry in reversed(chain)
                  if entry.get('type') == 'session_info' and _string(entry.get('name'))), '')
    parent = header.get('parentSession') if _string(header.get('parentSession')) and 0 < len(header['parentSession']) <= 4096 else None
    if archive:
        title = ('Pi 旧分支 · ' + title).rstrip(' ·')
    elif parent:
        title = ('Pi 导出分支 · ' + title).rstrip(' ·')
    indexer.save_session(connection, 'pi', source, title=title,
                         cwd=header.get('cwd') if _string(header.get('cwd')) else '',
                         parent=None, forked=original if archive else None, archived=archive,
                         started=indexer.timestamp(header.get('timestamp')), source_path=path)
    for ordinal, entry in enumerate(chain, 1):
        row = projected.get(entry['id'])
        if row is None:
            continue
        budget['messages'] += 1
        budget['projected_bytes'] += len(row['text'].encode('utf-8'))
        if budget['messages'] > MAX_PROJECTED_MESSAGES or budget['projected_bytes'] > MAX_PROJECTED_BYTES:
            raise OSError('Pi projected history limit exceeded')
        when, uid = indexer.timestamp(entry.get('timestamp')), entry['id']
        duplicate = bool(connection.execute('SELECT EXISTS(SELECT 1 FROM messages WHERE agent=? AND session_id=? AND message_id=?) OR EXISTS(SELECT 1 FROM native_image_messages WHERE agent=? AND session_id=? AND message_id=?)', ('pi', source, uid) * 2).fetchone()[0])
        if row['text'].strip() or row['phase'] == 'compact_summary':
            wrote = indexer.save_message(connection, 'pi', source, uid, row['role'], when,
                                         row['text'], path, entry['_line'], ordinal, row['phase'])
            _count(stats, 'pi_messages_added', wrote)
            _count(stats, 'duplicates_skipped', 1 - wrote)
            if row['phase'] in ('compact_summary', 'branch_summary'):
                _count(stats, 'pi_summaries_added', wrote)
        described = _images(row, indexer)
        if described['attachments']:
            _native_stats(stats)
            locator = {'kind': 'pi_jsonl', 'source_path': str(path), 'source_line': entry['_line'],
                       'record_sha256': entry['_record_sha256'],
                       'source_version': header.get('version') if type(header.get('version')) is int else None}
            indexer.save_native_attachments(connection, 'pi', source, uid, row['role'], when,
                                            row['text'], path, entry['_line'], ordinal, row['phase'],
                                            described, {'content': locator}, duplicate, stats)
            if not row['text'].strip() and not duplicate:
                _count(stats, 'pi_image_only_messages_added')
    effective, unsupported_compaction = _context(chain)
    context_ids, transforms, context_counts = [], {}, {}
    for entry in effective:
        # Prior compaction records in the kept interval are ignored natively.
        if entry.get('type') == 'compaction' and (not effective or entry is not effective[0]):
            continue
        row, more = _project(entry)
        for key, amount in more.items():
            _count(context_counts, key, amount)
        if row and entry['id'] in projected and not row['excluded']:
            context_ids.append(entry['id'])
            transforms[entry['id']] = row['transform']
            if len(context_ids) > PI_CONTEXT_MAX_IDS:
                raise OSError('Pi context coverage limit exceeded')
    record = dict(integrity, **counts, source_session_id=original, leaf_id=leaf['id'] if leaf else '',
                  branch_kind='archive' if archive else 'main',
                  source_version=header.get('version') if type(header.get('version')) is int else None,
                  text_messages=sum(bool(row['text'].strip()) for row in projected.values()),
                  image_only_messages=sum(not row['text'].strip() and bool(row['images']) for row in projected.values()),
                  summary_count=sum(row['phase'] in ('compact_summary', 'branch_summary') for row in projected.values()),
                  generated_context_messages=sum(row['phase'] in ('compact_summary', 'branch_summary', 'custom_context', 'bash_context') for row in projected.values()),
                  unsupported_compaction=unsupported_compaction, context_mode='text_projection',
                  context_message_ids=context_ids, context_transforms=transforms,
                  context_image_messages=sum(bool(projected[uid]['images']) for uid in context_ids),
                  context_omitted_tool_blocks=context_counts.get('omitted_tool_blocks', 0),
                  context_omitted_thinking_blocks=context_counts.get('omitted_thinking_blocks', 0),
                  context_omitted_image_blocks=context_counts.get('omitted_image_blocks', 0),
                  source_file_id=file_id, is_exported_branch=bool(parent),
                  parent_session_path=indexer.redact(parent)[:4096] if parent else '')
    record['continuable'] = bool(context_ids) and not any(record.get(key) for key in (
        'missing_parents', 'cycle', 'duplicate_ids', 'id_conflict', 'unparsed_lines', 'invalid_entries',
        'invalid_header', 'unsupported_version', 'ambiguous_copies',
        'unknown_context_records', 'unsupported_content_blocks', 'invalid_message_records', 'unsupported_compaction'))
    metadata[source] = record
    _count(stats, 'pi_sessions_added')
    if archive:
        _count(stats, 'pi_branches_added')


def _ingest_records(connection, records, bad, digest, signature, path, stats, indexer, metadata, seen, budget):
    headers = [entry for entry in records if entry.get('type') == 'session']
    header = headers[0] if headers else {}
    header_valid = (len(headers) == 1 and records and records[0] is header and _opaque(header.get('id'))
                    and _string(header.get('timestamp')) and bool(indexer.timestamp(header.get('timestamp')))
                    and _string(header.get('cwd')) and len(header.get('cwd', '')) <= 4096 and
                    ('parentSession' not in header or _string(header['parentSession']) and len(header['parentSession']) <= 4096))
    original = header['id'] if _opaque(header.get('id')) else 'pi-unidentified:' + hashlib.sha256(str(path).encode()).hexdigest()[:32]
    entries = [dict(entry) for entry in records if entry.get('type') != 'session']
    source = original
    copied = original in seen and seen[original] != str(path)
    if copied:
        for previous in metadata.values():
            if previous.get('source_session_id') == original:
                previous['ambiguous_copies'] += 1
                previous['continuable'] = False
        source = _source_id(original, 'copy', hashlib.sha256(str(path).encode()).hexdigest()[:24])
        _count(stats, 'pi_ambiguous_source_copies')
    seen.setdefault(original, str(path))
    nodes, alternatives = {}, []
    invalid, duplicates, conflicts = 0, 0, 0
    for entry in entries:
        uid = entry.get('id')
        if not _opaque(uid):
            invalid += 1
            uid = 'unverified:' + entry['_record_sha256'][:32] + ':line' + str(entry['_line'])
            entry['id'] = uid
        if ('parentId' not in entry or (entry.get('parentId') is not None and not _opaque(entry.get('parentId'))) or
                not _string(entry.get('timestamp')) or not indexer.timestamp(entry.get('timestamp')) or not _opaque(entry.get('type'))):
            invalid += 1
        if uid in nodes:
            duplicates += 1
            previous = {key: value for key, value in nodes[uid].items() if not key.startswith('_')}
            current = {key: value for key, value in entry.items() if not key.startswith('_')}
            conflicts += int(previous != current)
            alternatives.append(entry)
        else:
            nodes[uid] = entry
    roots = sum(entry.get('parentId') is None for entry in nodes.values())
    integrity = {'missing_parents': 0, 'cycle': False, 'duplicate_ids': duplicates,
                 'id_conflict': conflicts, 'unparsed_lines': bad, 'invalid_entries': invalid,
                 'invalid_header': int(not header_valid),
                 'unsupported_version': int(type(header.get('version')) is not int or header.get('version') != 3),
                 'multiple_roots': max(0, roots - 1), 'ambiguous_copies': int(copied),
                 'source_sha256': digest, 'source_signature': list(signature), 'header_count': len(headers)}
    leaf = entries[-1] if entries else None
    current_nodes = nodes
    if leaf is not None and nodes.get(leaf['id']) is not leaf:
        current_nodes = dict(nodes, **{leaf['id']: leaf})
    chain, missing, cycle = _walk(leaf, current_nodes, budget) if leaf else ([], 0, False)
    _save_branch(connection, source, original, header, leaf, chain,
                 dict(integrity, missing_parents=missing, cycle=cycle), False, path, stats, indexer, metadata, budget)
    parents = {entry.get('parentId') for entry in nodes.values() if isinstance(entry.get('parentId'), str)}
    candidates = [entry for uid, entry in nodes.items() if uid not in parents and (leaf is None or entry is not nodes.get(leaf['id']))]
    covered = {entry['id'] for entry in chain}
    # Cycles have no leaf: still preserve every disconnected readable record.
    candidate_ids = {entry['id'] for entry in candidates}
    candidates.extend(entry for entry in sorted(nodes.values(), key=lambda value: value['_line'], reverse=True)
                      if entry['id'] not in covered and entry['id'] not in candidate_ids)
    for old_leaf in candidates:
        if old_leaf['id'] in covered:
            continue
        old_chain, old_missing, old_cycle = _walk(old_leaf, nodes, budget)
        covered.update(entry['id'] for entry in old_chain)
        if not any((row := _project(entry)[0]) and (row['text'].strip() or row['images']) for entry in old_chain):
            continue
        branch = _source_id(source, 'branch', old_leaf['id'])
        _save_branch(connection, branch, original, header, old_leaf, old_chain,
                     dict(integrity, missing_parents=old_missing, cycle=old_cycle), True, path, stats, indexer, metadata, budget)
    if leaf is not None and nodes.get(leaf['id']) is not leaf:
        alternatives.append(nodes[leaf['id']])
    for alternative in alternatives:
        if leaf is alternative:
            continue
        own = dict(nodes, **{alternative['id']: alternative})
        old_chain, old_missing, old_cycle = _walk(alternative, own, budget)
        anchor = alternative['id'] + ':record:' + alternative['_record_sha256'][:16] + ':line' + str(alternative['_line'])
        _save_branch(connection, _source_id(source, 'branch', anchor), original, header, alternative, old_chain,
                     dict(integrity, missing_parents=old_missing, cycle=old_cycle), True, path, stats, indexer, metadata, budget)


def ingest_pi(connection, root, stats, *, indexer):
    """Index only explicitly selected Pi agent-root/sessions into caller stage.

    The publisher must discard a stage with source_errors. Incomplete JSON/tree
    semantics remain browsable with continuable=false; they are not IO success
    claims. No native manager, credentials, model, extension or parentSession
    file is accessed. Full refresh stages must contain no old Pi rows.
    """
    root = Path(os.path.abspath(Path(root).expanduser()))
    metadata, seen = {}, {}
    budget = dict(bytes=0, records=0, steps=0, branches=0, messages=0, projected_bytes=0)
    iterator = None
    try:
        iterator = _files(root)
        for number, (directory, name, path) in enumerate(iterator, 1):
            if number > MAX_FILES:
                raise OSError('Pi source file count exceeded')
            _count(stats, 'pi_files')
            records, bad, digest, signature = _read_file(directory, name, path, stats, budget)
            _ingest_records(connection, records, bad, digest, signature, path, stats, indexer, metadata, seen, budget)
    except FileNotFoundError:
        # Initial optional-root absence is handled by _files. Once enumeration
        # started, a disappearing directory/file always invalidates the stage.
        _count(stats, 'source_errors')
    except (OSError, ValueError, TypeError, RecursionError, UnicodeEncodeError):
        _count(stats, 'source_errors')
    finally:
        if iterator is not None:
            iterator.close()
    # Stream the map encoder under the same byte limit as the public reader;
    # publishing a map it cannot validate would disable every Pi continuation.
    pieces, size = [], 0
    for piece in json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':')).iterencode(metadata):
        size += len(piece.encode('utf-8'))
        if size > PI_COVERAGE_MAX_BYTES:
            _count(stats, 'source_errors')
            return
        pieces.append(piece)
    connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (META_KEY, ''.join(pieces)))
