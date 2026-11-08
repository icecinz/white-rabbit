"""Read the single editorially maintained briefing through the host's text reader."""
from datetime import date
import hashlib
import re


PATH = '/api/management/briefing'
DOCUMENT = '文档/简报/当前简报.md'
MAX_BYTES = 64 * 1024


def _parse(text):
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('briefing_too_large')
    title, date_line, summary, body = (text.lstrip('\ufeff').strip() + '\n\n\n').split('\n', 3)
    title = re.fullmatch(r'# ([^\r\n]{1,120})', title.strip())
    date_line = re.fullmatch(r'日期[：:]\s*(\d{4}-\d{2}-\d{2})', date_line.strip())
    summary = re.fullmatch(r'摘要[：:]\s*([^\r\n]{1,500})', summary.strip())
    body = body.strip()
    sources = re.search(r'(?m)^## 来源\s*\n([\s\S]+)$', body)
    if not (title and title[1].strip() and date_line and summary and summary[1].strip()
            and sources and sources[1].strip() and body[:sources.start()].strip()):
        raise ValueError('briefing_invalid')
    return {'title': title[1].strip(), 'date': date.fromisoformat(date_line[1]).isoformat(),
            'summary': summary[1].strip(), 'body': body,
            'revision': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def snapshot(workspace_id, root, reader):
    if root is None:
        return {'workspace_id': workspace_id, 'briefing': None, 'status': 'unavailable'}
    try:
        document = reader(root, DOCUMENT, missing_ok=True)
        if document is None:
            return {'workspace_id': workspace_id, 'briefing': None, 'status': 'empty'}
        briefing = _parse(document['content'])
        if document.get('snapshot') is True:
            briefing['snapshot_at'] = document.get('updated_at')
        return {'workspace_id': workspace_id, 'briefing': briefing, 'status': 'ready'}
    except (ValueError, OSError):
        return {'workspace_id': workspace_id, 'briefing': None, 'status': 'unavailable'}
