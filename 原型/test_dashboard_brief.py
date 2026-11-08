"""Read only the designated briefing in a disposable project."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dashboard_brief
import install_local
from server import DocumentAccessDenied, Service, documents_root, read_local_document


class BriefingTests(unittest.TestCase):
    def test_empty_then_one_version_with_body_and_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = documents_root(Path(temp).resolve())
            snapshot = lambda: dashboard_brief.snapshot('workspace-fixture', root, read_local_document)
            self.assertEqual(snapshot(), {'workspace_id': 'workspace-fixture', 'briefing': None, 'status': 'empty'})
            target = root / dashboard_brief.DOCUMENT
            target.parent.mkdir(parents=True)
            target.write_text('# 虚构标题\n日期：2026-11-08\n摘要：验收摘要。\n\n'
                              '正文保留 **粗体**。\n\n## 来源\n- [虚构来源](https://example.invalid/)\n', encoding='utf-8')
            first = snapshot()['briefing']
            self.assertEqual((first['title'], first['date'], first['summary']), ('虚构标题', '2026-11-08', '验收摘要。'))
            self.assertIn('正文保留 **粗体**。', first['body'])
            self.assertIn('https://example.invalid/', first['body'])
            target.write_text(target.read_text().replace('正文保留', '正文改成'), encoding='utf-8')
            self.assertNotEqual(first['revision'], snapshot()['briefing']['revision'])

    def test_bad_editorial_input_and_symlink_never_become_a_story(self):
        with tempfile.TemporaryDirectory() as temp:
            root = documents_root(Path(temp).resolve())
            target = root / dashboard_brief.DOCUMENT
            target.parent.mkdir(parents=True)
            target.write_text('# 草稿\n日期：2026-02-31\n摘要：摘要\n\n正文\n\n## 来源\n- 说明', encoding='utf-8')
            expected = {'workspace_id': 'workspace-fixture', 'briefing': None, 'status': 'unavailable'}
            self.assertEqual(dashboard_brief.snapshot('workspace-fixture', root, read_local_document), expected)
            other = root / '候选.md'
            other.write_text('# 不该发布\n日期：2026-11-08\n摘要：摘要\n\n正文\n\n## 来源\n- 说明', encoding='utf-8')
            target.unlink()
            target.symlink_to(other)
            self.assertEqual(dashboard_brief.snapshot('workspace-fixture', root, read_local_document), expected)

    def test_foreground_mirror_is_labeled_and_not_used_for_live_missing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / 'project'
            source = root / 'prototype'
            source.mkdir(parents=True)
            target = root / dashboard_brief.DOCUMENT
            target.parent.mkdir(parents=True)
            target.write_text('# 镜像稿\n日期：2026-11-08\n摘要：测试。\n\n正文。\n\n## 来源\n- 虚构来源\n', encoding='utf-8')
            layout = install_local.Layout(base / 'home')
            install_local.refresh_document_cache(layout, source)
            service = SimpleNamespace(workspace_id='workspace-fixture', documents_dir=root,
                                      documents_cache_dir=layout.documents)
            with patch('server.read_local_document', side_effect=DocumentAccessDenied('fixture denied')):
                result = Service.dashboard_briefing(service)
            self.assertEqual(result['briefing']['title'], '镜像稿')
            self.assertTrue(result['briefing']['snapshot_at'])
            target.unlink()
            self.assertEqual(Service.dashboard_briefing(service)['status'], 'empty')
