"""Small reference index for application-attached native inputs.

Capture is best effort after a successful native dispatch, never part of its
transaction. Listing reads saved refs and receipt metadata only: no owner,
native history, prompt, memory body or workspace-output scan.
"""
import hashlib
import json
import re
import uuid

from native_materials import validate_refs
from rabbit_content.file_task_store import FileTaskStoreError
from rabbit_runtime.native_session_store import request_sha256, validate_token


AGENTS = {'codex': 'native_chats', 'pi': 'pi_native_chats',
          'hermes': 'hermes_native_chats', 'claude': 'claude_native_chats',
          'dsh': 'dsh_native_chats'}
ROOTS = {'codex': '/api/native-chats', **{a: '/api/'+a+'-chats' for a in AGENTS if a != 'codex'}}
STATUSES = frozenset(('submitted_unknown', 'accepted', 'rejected', 'completed', 'failed', 'interrupted'))
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_RECORDS = 512
MAX_ITEMS = 2048


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value):
    try:
        valid = type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('需要准确的原生对话编号。')
    return value


def _sha(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise ValueError('原生材料引用身份需要核对。')
    return value


def receipt(manager, agent, chat_id, client_id):
    """Only an existing local operation; never dispatch/read a native view."""
    validate_token(client_id)
    if agent == 'codex':
        row = manager.store.read_operation(client_id, session_key='chat/codex/'+chat_id)
        if row is not None and row.get('kind') != 'turn_start':
            raise ValueError('材料编号不是原生消息请求。')
    else:
        row = manager.store.operation(client_id)
        if row is not None and row['chat_id'] != chat_id:
            raise ValueError('材料回执属于其他原生对话。')
    if row is None:
        return None
    if row['client_id'] != client_id or row['status'] not in STATUSES:
        raise ValueError('原生材料回执需要核对。')
    return dict(client_id=client_id, request_sha256=_sha(row['request_sha256']), status=row['status'])


class NativeMaterialIndex:
    def __init__(self, store, workspace_id):
        self.store, self.workspace_id = store, _sha(workspace_id)
        with store.lock, store._open() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_input_material_refs (
                workspace_id TEXT NOT NULL, agent TEXT NOT NULL, chat_id TEXT NOT NULL,
                client_id TEXT NOT NULL, raw_request_sha256 TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, material_refs TEXT NOT NULL,
                PRIMARY KEY(workspace_id,agent,client_id))''')

    def save(self, agent, chat_id, client_id, raw_sha, native_sha, refs):
        if agent not in AGENTS: raise ValueError('原生 Agent 无效。')
        _uuid(chat_id); validate_token(client_id); _sha(raw_sha); _sha(native_sha)
        refs = validate_refs(refs)
        if not refs: return
        encoded = _json(refs)
        if len(encoded.encode('utf-8')) > 4096: raise ValueError('材料引用过多。')
        row = (self.workspace_id, agent, chat_id, client_id, raw_sha, native_sha, encoded)
        with self.store.lock, self.store._open() as db:
            old = db.execute('SELECT workspace_id,agent,chat_id,client_id,raw_request_sha256,'
                'request_sha256,material_refs FROM native_input_material_refs '
                'WHERE workspace_id=? AND agent=? AND client_id=?', row[:2]+(client_id,)).fetchone()
            if old is not None:
                if tuple(old) != row: raise ValueError('材料引用与原请求不一致。')
                return
            db.execute('INSERT INTO native_input_material_refs VALUES(?,?,?,?,?,?,?)', row)

    def rows(self, agent, chat_id):
        if agent not in AGENTS: raise ValueError('原生 Agent 无效。')
        _uuid(chat_id)
        with self.store.lock, self.store._open(readonly=True) as db:
            rows = db.execute('SELECT client_id,raw_request_sha256,request_sha256,'
                'length(CAST(material_refs AS BLOB)) AS size, '
                'CASE WHEN length(CAST(material_refs AS BLOB))<=4096 THEN material_refs END AS material_refs '
                'FROM native_input_material_refs WHERE workspace_id=? AND agent=? AND chat_id=? '
                'ORDER BY client_id LIMIT ?', (self.workspace_id, agent, chat_id, MAX_RECORDS+1)).fetchall()
        if len(rows) > MAX_RECORDS: raise ValueError('材料清单超过一次读取容量，没有截断返回。')
        result = []
        for row in rows:
            validate_token(row['client_id']); _sha(row['raw_request_sha256']); _sha(row['request_sha256'])
            if not 0 < row['size'] <= 4096: raise ValueError('原生材料引用需要核对。')
            refs = validate_refs(json.loads(row['material_refs']))
            if not refs: raise ValueError('原生材料引用需要核对。')
            result.append(dict(client_id=row['client_id'],raw_request_sha256=row['raw_request_sha256'],
                               request_sha256=row['request_sha256'],material_refs=refs))
        return result


def dispatch(service, agent, manager, method, path, *, query='', body=None):
    """A rejected/replayed request can never backfill refs onto an old receipt."""
    candidate = None
    if method == 'POST' and not query and type(body) is dict and path.endswith('/send'):
        try:
            chat_id = _uuid(path.removeprefix(ROOTS[agent]+'/').removesuffix('/send'))
            fields = {k: v for k, v in body.items() if k not in ('_one_ai_csrf', 'track_result')}
            refs = validate_refs(fields.get('material_refs', []))
            client = fields['client_id']
            if refs and receipt(manager, agent, chat_id, client) is None:
                candidate = (chat_id, client, request_sha256(fields), refs)
        except Exception:
            pass  # Native validation and delivery remain the owner's job.
    result = manager.dispatch(method, path, query=query, body=body)
    if candidate is not None and type(result) is dict and result.get('reused') is not True:
        try:
            chat_id, client, raw_sha, refs = candidate
            current = receipt(manager, agent, chat_id, client)
            # A frozen intent proves which raw app request produced this new
            # successful dispatch. It alone never proves native acceptance.
            intent = result.get('material_intent') if agent in ('claude', 'dsh') else result.get('profile_intent')
            if (current is not None and type(intent) is dict
                    and intent.get('chat_id') == chat_id and intent.get('client_id') == client
                    and intent.get('raw_request_sha256' if agent in ('claude', 'dsh') else 'request_sha256') == raw_sha
                    and (agent not in ('claude', 'dsh') or intent.get('request_sha256') == current['request_sha256'])):
                service._native_material_index.save(agent, chat_id, client, raw_sha, current['request_sha256'], refs)
        except Exception:
            pass  # Never turn a successful native send into a failed response.
    return result


def _snapshot(service, agent, chat_id, manager):
    rows = {row['client_id']: row for row in service._native_material_index.rows(agent, chat_id)}
    intents = getattr(manager, 'material_intents', None) if agent in ('claude','dsh') else None
    if intents is not None:
        for row in intents.list_for_chat(chat_id):
            old = rows.get(row['client_id'])
            if old is not None and any(old[key] != row[key] for key in ('raw_request_sha256','request_sha256','material_refs')):
                raise ValueError('原生材料引用与原请求不一致。')
            rows[row['client_id']] = row
    if len(rows) > MAX_RECORDS or sum(len(row['material_refs']) for row in rows.values()) > MAX_ITEMS:
        raise ValueError('材料清单超过一次读取容量，没有截断返回。')
    result, unresolved = [], []
    for client, row in sorted(rows.items()):
        current = receipt(manager, agent, chat_id, client)
        if current is None or current['request_sha256'] != row['request_sha256']:
            unresolved.append(client)
            continue
        result.append(dict(**current, material_refs=row['material_refs']))
    return dict(records=result, unresolved=unresolved)


def inventory(service, agent, chat_id):
    if type(agent) is not str or agent not in AGENTS: raise ValueError('请选择一个已接入的原生 Agent。')
    chat_id = _uuid(chat_id)
    manager = getattr(service, AGENTS[agent], None)
    if manager is None: raise ValueError('这个原生 Agent 尚未接入。')
    manager.catalogue.get(chat_id)
    snapshot = _snapshot(service, agent, chat_id, manager)
    records = []
    def label(item):
        name, size = item['name'], item['size_bytes']
        if (type(name) is not str or not 0 < len(name) <= 255 or any(ord(c)<32 or ord(c)==127 for c in name)
                or type(size) is not int or not 0 <= size <= 2**53-1): raise ValueError
        name.encode('utf-8')
        return dict(name=name, size_bytes=size)
    for source in snapshot['records']:
        record = {key: source[key] for key in ('client_id','request_sha256','status')}
        record.update(materials=[], unavailable=[])
        for ref in source['material_refs']:
            reason = 'metadata_unavailable'
            try:
                if ref['kind'] == 'image':
                    item = service.materials.metadata_image(ref['id'])
                    if item['sha256'] != ref['sha256']:
                        reason = 'metadata_changed'; raise ValueError
                else:
                    item = service.file_tasks.input_snapshot(ref['id'], chat_id=ref.get('source_chat_id'),
                        expected_sha256=ref['sha256'], content=False)['artifact']
                    if item.get('available') is not True: raise ValueError
                record['materials'].append(dict(ref=ref, origin='input', **label(item)))
            except (ValueError, OSError, KeyError, TypeError, AttributeError, FileTaskStoreError):
                record['unavailable'].append(dict(kind=ref['kind'], id=ref['id'], origin='input', reason=reason))
        records.append(record)
    if snapshot != _snapshot(service, agent, chat_id, manager):
        raise ValueError('原生材料或回执正在变化，请重新读取清单。')
    if len(records)>MAX_RECORDS or sum(len(r['materials'])+len(r['unavailable']) for r in records)>MAX_ITEMS:
        raise ValueError('材料清单超过一次读取容量，没有截断返回。')
    result = dict(workspace_id=service.workspace_id, chat_id=chat_id, agent=agent,
        scope='native_chat_materials', source_revision=hashlib.sha256(_json(snapshot).encode()).hexdigest(),
        content_verification='not_performed', coverage='recorded_inputs_only', unrecorded_inputs_possible=True,
        complete=not snapshot['unresolved'] and not any(r['unavailable'] for r in records),
        unverified_client_ids=snapshot['unresolved'], records=records)
    if len(_json(result).encode('utf-8'))>512*1024: raise ValueError('材料清单超过一次读取容量，没有截断返回。')
    return result
