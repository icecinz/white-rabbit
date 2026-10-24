"""Prepare frozen text inputs through one explicit storage read port.

The caller owns storage and supplies the reader. This module never opens a
file/database, chooses an Agent, starts a process or sends a model request.
"""
from typing import Protocol

from rabbit_content import FileTaskStoreError


class TextSnapshotReader(Protocol):
    """Return a verified artifact snapshot; unavailable versions raise FileTaskStoreError.

    content=False returns metadata without decoding the text. content=True
    additionally returns the complete UTF-8 snapshot in the top-level text key.
    The reader must enforce its own object ownership, SHA and I/O bounds.
    """
    def __call__(self, identifier: str, *, chat_id=None, expected_sha256=None, content=True) -> dict: ...


def prepare_text_file_messages(messages, pinned_ids, intent, current_refs, *, read_snapshot: TextSnapshotReader,
                               max_file_bytes=512 * 1024, max_total_bytes=8 * 1024 * 1024, max_files=128):
    """Project frozen text versions, bounded both in IO and rendered bytes.

    One cached body can occur in many records, so every occurrence consumes
    the projection bound. Required turns take priority; optional records
    that cannot be prepared remain whole records marked unavailable.
    """
    messages = [dict(message) for message in messages]
    if intent['mode'] == 'chat' and intent['artifact_ids']:
        for message in reversed(messages):
            if message.get('is_draft') and message.get('role') == 'user':
                message['text_file_inputs'] = [
                    {'id': item['id'], 'sha256': item['sha256']} for item in current_refs]
                message['text_file_input_refs'] = list(current_refs)
                break
    candidates = [i for i, message in enumerate(messages)
                  if message.get('role') == 'user' and message.get('status', 'completed') in ('completed', 'imported')
                  and message.get('text_file_inputs')]
    pins = set(pinned_ids or ())
    required = [i for i in candidates if messages[i].get('is_draft')]
    required += [i for i in candidates if messages[i].get('id') in pins and i not in required]
    priority = required + [i for i in reversed(candidates) if i not in required]
    cache, inspected = {}, set()
    read_bytes = projected_bytes = 0

    def missing(frozen, refs, reason, material=None):
        ref = next((item for item in refs if isinstance(item, dict) and item.get('id') == frozen['id']), {})
        value = dict(frozen, reason=reason)
        for key in ('name', 'size_bytes', 'source'):
            if material is not None and key in material:
                value[key] = material[key]
            elif key in ref:
                value[key] = ref[key]
        return value

    for i in priority:
        message = messages[i]
        frozen = list(message['text_file_inputs'])
        refs = message.get('text_file_input_refs') or []
        pending, unavailable = [], []
        for item in frozen:
            key = (item['id'], item['sha256'])
            ref = next((row for row in refs if isinstance(row, dict) and row.get('id') == item['id']), {})
            scope = message.get('chat_id') or ref.get('chat_id')
            if key in cache:
                result = cache[key]
            elif key not in inspected and len(inspected) >= max_files:
                result = {'missing': missing(item, refs, 'preparation_capacity')}
            else:
                inspected.add(key)
                try:
                    material = read_snapshot(item['id'], chat_id=scope,
                        expected_sha256=item['sha256'], content=False)['artifact']
                    if not material.get('available') or not material.get('content_verified'):
                        result = {'missing': missing(item, refs, 'unverified', material)}
                    elif (type(material.get('size_bytes')) is not int
                          or not 0 <= material['size_bytes'] <= max_file_bytes):
                        result = {'missing': missing(item, refs, 'changed', material)}
                    else:
                        result = {'material': material, 'scope': scope}
                except FileTaskStoreError as error:
                    result = {'missing': missing(item, refs, 'not_found' if error.code == 'not_found' else 'changed')}
                cache[key] = result
            if 'missing' in result:
                unavailable.append(dict(result['missing']))
            else:
                pending.append((item, key, result))
        size = sum(result['material']['size_bytes'] for _, _, result in pending)
        new_size = sum(result['material']['size_bytes'] for _, _, result in pending if 'text' not in result)
        # A record is atomic: partial material preparation is never used as
        # the user's whole original record. Cache reduces IO, not its size.
        capacity_blocked = (projected_bytes + size > max_total_bytes
                            or read_bytes + new_size > max_total_bytes)
        if capacity_blocked:
            unavailable.extend(missing(item, refs, 'preparation_capacity', result['material'])
                               for item, _, result in pending)
        prepared = []
        if not capacity_blocked:
            for item, key, result in pending:
                try:
                    if 'text' not in result:
                        # Even a damaged snapshot can consume the low-level
                        # reader's maximum before hash validation fails.
                        # Reserve that worst case before any uncached IO.
                        if read_bytes + max_file_bytes + 1 > max_total_bytes:
                            unavailable.append(missing(item, refs, 'preparation_capacity', result['material']))
                            continue
                        snapshot = read_snapshot(item['id'], chat_id=result['scope'],
                            expected_sha256=item['sha256'])
                        actual_bytes = len(snapshot['text'].encode('utf-8'))
                        read_bytes += actual_bytes
                        if actual_bytes != result['material']['size_bytes']:
                            raise FileTaskStoreError('artifact_changed')
                        result['text'] = snapshot['text']
                    public = {key: result['material'][key] for key in ('id', 'name', 'sha256', 'size_bytes', 'source')}
                    prepared.append(dict(public, text=result['text']))
                except FileTaskStoreError as error:
                    read_bytes += max_file_bytes + 1
                    unavailable.append(missing(item, refs, 'not_found' if error.code == 'not_found' else 'changed', result['material']))
                    cache[key] = {'missing': unavailable[-1]}
        # A missing peer causes Context to omit the whole record, but the
        # other verified identities remain visible in its evidence counts.
        # No fake or truncated body stands in for those healthy materials.
        projected_bytes += sum(item['size_bytes'] for item in prepared)
        if prepared:
            message['text_file_materials'] = prepared
        else:
            message.pop('text_file_materials', None)
        if unavailable:
            message['missing_text_file_materials'] = unavailable
        else:
            message.pop('missing_text_file_materials', None)
        message.pop('text_file_inputs', None)
        message.pop('text_file_input_refs', None)
    return messages

