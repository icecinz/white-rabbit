"""Bounded, short-lived references to the exact project text already shown."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
import re
import secrets
import threading
import time
import uuid

MAX_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_TICKETS = 64
LIFETIME_SECONDS = 600


class DocumentTicketExpired(ValueError):
    pass


class DocumentTickets:
    def __init__(self, *, clock=time.monotonic):
        self.clock, self.lock = clock, threading.RLock()
        self.entries = OrderedDict()

    def _prune(self):
        now = self.clock()
        for key in list(self.entries):
            if self.entries[key]['expires'] <= now:
                del self.entries[key]

    def issue(self, document, relative_path):
        if (not isinstance(document, dict) or not isinstance(document.get('content'), str)
                or not isinstance(document.get('name'), str)
                or not isinstance(relative_path, str) or not relative_path
                or len(relative_path) > 8192 or relative_path.startswith('/')
                or any(part in ('', '.', '..') or part.startswith('.') for part in relative_path.split('/'))
                or '\\' in relative_path or any(ord(char) < 32 for char in relative_path)):
            raise ValueError('这份项目预览不能用作文件输入。')
        raw = document['content'].encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('这份文件超过512 KiB，没有放入文件输入。')
        provenance = {'kind': 'project_preview', 'relative_path': relative_path,
                      'snapshot': document.get('snapshot') is True,
                      'updated_at': document.get('updated_at')}
        value = {'name': document['name'], 'text': document['content'], 'size_bytes': len(raw),
                 'sha256': hashlib.sha256(raw).hexdigest(), 'provenance': provenance}
        return self._issue(value)

    def issue_markdown(self, document):
        """Freeze a trusted registry current_file result, never a free path."""
        if type(document) is not dict:
            raise ValueError('这份Markdown文件预览不能用作材料。')
        path, name, content = document.get('path'), document.get('name'), document.get('content')
        if (type(path) is not str or not 1 <= len(path) <= 240 or len(path.split('/')) > 8
                or '\\' in path or ':' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or any(not part or part.startswith('.') for part in path.split('/'))
                or not path.casefold().endswith('.md') or type(name) is not str
                or name != PurePosixPath(path).name or type(content) is not str or '\0' in content):
            raise ValueError('这份Markdown文件预览不能用作材料。')
        try:
            for field in ('binding_id', 'head_id'):
                if type(document.get(field)) is not str or str(uuid.UUID(document[field])) != document[field]:
                    raise ValueError
            if any(len(part.encode('utf-8')) > 255 for part in path.split('/')):
                raise ValueError
            raw = content.encode('utf-8')
        except (ValueError, UnicodeError):
            raise ValueError('这份Markdown文件预览身份或文字无效。') from None
        digest = hashlib.sha256(raw).hexdigest()
        if (len(raw) > MAX_BYTES or type(document.get('size_bytes')) is not int
                or document['size_bytes'] != len(raw) or type(document.get('sha256')) is not str
                or re.fullmatch(r'[0-9a-f]{64}', document['sha256']) is None
                or document['sha256'] != digest):
            raise ValueError('这份Markdown文件预览全文或大小无法核对。')
        origin = {'kind': 'markdown_preview', 'binding_id': document['binding_id'],
                  'head_id': document['head_id'], 'relative_path': path, 'sha256': digest}
        return self._issue({'name': name, 'text': content, 'size_bytes': len(raw), 'sha256': digest,
                            'provenance': {'kind': 'upload', 'from_markdown': origin}})

    def _issue(self, value):
        with self.lock:
            self._prune()
            while self.entries and (len(self.entries) >= MAX_TICKETS
                    or sum(item['value']['size_bytes'] for item in self.entries.values()) + value['size_bytes'] > MAX_TOTAL_BYTES):
                self.entries.popitem(last=False)
            ticket = secrets.token_hex(32)
            self.entries[ticket] = {'expires': self.clock() + LIFETIME_SECONDS, 'value': value}
        return ticket

    def get(self, ticket):
        if not isinstance(ticket, str) or len(ticket) != 64 or any(char not in '0123456789abcdef' for char in ticket):
            raise ValueError('所见预览引用无效，请重新预览后明确选择。')
        with self.lock:
            self._prune()
            row = self.entries.get(ticket)
            if row is None:
                raise DocumentTicketExpired('所见预览已过期或服务曾重启，请重新预览后明确选择；没有自动改取新版本。')
            value = row['value']
            return deepcopy(value)

    def clear(self):
        with self.lock:
            self.entries.clear()
