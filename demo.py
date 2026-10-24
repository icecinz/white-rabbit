#!/usr/bin/env python3
"""Temporary local demo: invented history and rule-based replies, no real Agent."""
import sys

if sys.version_info < (3, 11):
    raise SystemExit('White Rabbit requires Python 3.11+. Try python3.12 demo.py if your system python3 is older.')

from contextlib import contextmanager
import argparse
import json
from pathlib import Path
import re
import sqlite3
import tempfile

# Keep the same source layout as the application; no installed copy is used.
PROTOTYPE = Path(__file__).resolve().parent / '原型'
if str(PROTOTYPE) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE))

from rabbit_preparation.context import CONVERSATION_OPEN, CONVERSATION_CLOSE
from server import Handler, INDEXER, Service, ThreadingHTTPServer

HISTORY_ID = 'demo-paper-boat'
MODEL = 'demo-context'


class DemoAdapter:
    """Read the application's handed-off JSONL; never launch or call a model."""
    def __init__(self, label):
        self.label = label

    def available(self):
        return {'available': True, 'model': MODEL, 'provider': '本地规则模拟',
                'reason': '演示连接：不启动Agent，不调用模型。', 'connection': 'local'}

    def run(self, prompt, cwd, on_event, cancel_event, route=None):
        if cancel_event.is_set():
            raise InterruptedError('已停止模拟回答。')
        try:
            body = prompt.split(CONVERSATION_OPEN, 1)[1].rsplit(CONVERSATION_CLOSE, 1)[0]
            records = [json.loads(line) for line in body.splitlines() if line.strip()]
            text = '\n'.join(record['content'] for record in records
                             if type(record) is dict and type(record.get('content')) is str)
            codes = list(dict.fromkeys(re.findall(r'纸船[0-9]{4}', text)))
        except (IndexError, TypeError, ValueError):
            codes = []
        detail = ('从本轮共同上下文读到：' + '、'.join(codes) + '。' if codes else
                  '本轮共同上下文里没有纸船代号；可打开演示对话再试。')
        reply = '【模拟回答】' + self.label + '：' + detail + '这是规则演示，没有调用真实模型。'
        on_event({'type': 'text', 'text': reply})
        return {'text': reply, 'model': MODEL, 'provider': '本地规则模拟'}


class DemoService(Service):
    def status(self):
        return dict(super().status(), demo_mode=True)

    def refresh(self):
        # The fixed invented index is already complete. Never discover HOME.
        return {'ok': True, 'demo_mode': True, 'stats': self.history.stats()}

    def management_inventory(self, fresh=False):
        result = super().management_inventory(fresh=fresh)
        result['device']['name'] = '演示设备'
        result['notice'] = '仅演示模拟连接；未接入真实Agent、模型、历史或长期记忆。'
        for agent in result['agents']:
            agent['name'] += '（模拟）'
            agent['check_scope'] = '纯本地规则模拟；没有读取登录或模型凭据。'
        for model in result['models']:
            model['source'] = '演示用固定候选，不是真实模型'
        return result


def create_demo_service(root):
    """Build only inside a newly allocated, canonical temporary directory."""
    root = Path(root).resolve(strict=True)
    index, documents = root / 'history.sqlite', root / 'documents'
    if index.exists() or documents.exists():
        raise FileExistsError('演示需要一个新的临时资料目录。')
    documents.mkdir(mode=0o700)
    document = documents / '演示说明.md'
    document.write_text('# 小白兔演示说明\n\n所有聊天和回答都是虚构的。\n\n'
                        '1. 在演示对话中问“之前的代号是什么？”\n'
                        '2. 切换Codex（模拟）与Hermes（模拟），继续问相同问题。\n'
                        '3. 刷新会保留本次演示内容；退出程序后全部删除。\n', encoding='utf-8')
    with sqlite3.connect(index) as db:
        INDEXER.create_schema(db)
        INDEXER.save_session(db, 'codex', HISTORY_ID, title='虚构旧对话 · 纸船7318',
                             started='2026-01-01T00:00:00Z', source_path='demo')
        records = [('user', '这是虚构旧聊天：约定的代号是纸船7318。'),
                   ('assistant', '【模拟回答】可以换一个模拟助手继续问代号。\n\n'
                    '[演示说明](<' + str(document) + '>)')]
        for number, (role, content) in enumerate(records, 1):
            INDEXER.save_message(db, 'codex', HISTORY_ID, 'demo-'+str(number), role,
                                 '2026-01-01T00:00:0'+str(number)+'Z', content, 'demo', number, number,
                                 'final_answer' if role == 'assistant' else '')
        db.execute("UPDATE sessions SET message_count=2,updated_at='2026-01-01T00:00:02Z'")
        db.execute("INSERT INTO meta VALUES('built_at','2026-01-01T00:00:02Z')")
        db.execute("INSERT INTO meta VALUES('stats','{}')")
    service = DemoService(root / 'data', index,
        adapters={'codex': DemoAdapter('Codex（模拟）'), 'hermes': DemoAdapter('Hermes（模拟）')},
        documents_dir=documents, memory_driver=None, native_source_getter=None, file_adapters={},
        codex_root=root / 'absent-codex', hermes_db=root / 'absent-hermes.db',
        claude_root=root / 'absent-claude', pi_root=root / 'absent-pi')
    try:
        for backend, identity in service.default_profile_ids.items():
            profile = service.profiles.get(identity)
            service.rename_ai_profile(identity, {'revision': profile['revision'],
                'name': ('Codex' if backend == 'codex' else 'Hermes') + '（模拟）'})
        service.create({'title': '演示 · 换个助手继续',
                        'source': {'agent': 'codex', 'session_id': HISTORY_ID}})
    except BaseException:
        service.shutdown()
        raise
    return service


@contextmanager
def demo_session():
    with tempfile.TemporaryDirectory(prefix='xiaobaitu-demo-') as directory:
        service = create_demo_service(Path(directory).resolve())
        try:
            yield service
        finally:
            service.shutdown()


class DemoHandler(Handler):
    timeout = 5


def main(argv=None):
    parser = argparse.ArgumentParser(description='小白兔临时演示：仅虚构数据，不调用真实模型')
    parser.add_argument('--port', type=int, default=17862)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('端口需要在1024–65535之间。')
    with demo_session() as service:
        http = ThreadingHTTPServer(('127.0.0.1', args.port), DemoHandler)
        http.service = service
        try:
            print('小白兔演示：http://127.0.0.1:' + str(args.port) + '/', flush=True)
            print('仅虚构聊天与【模拟回答】；按Ctrl+C退出并删除本次演示资料。', flush=True)
            http.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            http.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
