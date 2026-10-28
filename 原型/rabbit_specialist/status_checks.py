"""Independent manual status-check records; no scheduler or configuration writes.

Facts are frozen before analysis. Only the first facts_ready -> analyzing
transition grants submission. Reads never submit, retry, or change records.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
import uuid

from rabbit_runtime.native_json import canonical_metadata, NativeJSONError


FACT_IDS = ('configuration', 'catalogue', 'tools', 'memory')
FACT_STATES = frozenset(('observed', 'unverified', 'skipped'))
JUDGMENTS = frozenset(('consistent', 'review_needed', 'unverified', 'not_applicable'))
ACTIVE = ('collecting', 'facts_ready', 'analyzing')
STAGES = frozenset((*ACTIVE, 'completed', 'partial', 'analysis_failed', 'unconfirmed'))
_FACT_FIELDS = frozenset(('id', 'label', 'state', 'source', 'observed_at', 'read_at',
                          'identity', 'data', 'evidence_sha256'))
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9_.:/-]{1,160}\Z')
_COLUMNS = ('check_id', 'workspace_id', 'owner_generation', 'scope_sha256', 'target_json',
            'stage', 'facts_json', 'analysis_json', 'created_at', 'published_at', 'error_code')
_DDL = '''CREATE TABLE IF NOT EXISTS status_checks (
    check_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_generation TEXT NOT NULL,
    scope_sha256 TEXT NOT NULL, target_json TEXT NOT NULL, stage TEXT NOT NULL,
    facts_json TEXT, analysis_json TEXT, created_at TEXT NOT NULL,
    published_at TEXT, error_code TEXT)'''


class StatusCheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Status check: ' + code)


def _fail(code='invalid_arguments'):
    raise StatusCheckError(code)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or '\0' in value
            or not empty and not value.strip()):
        _fail()
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        _fail()
    return value


def _timestamp(value):
    _text(value, 100)
    try:
        if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            _fail()
    except ValueError:
        _fail()
    return value


def _id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()
    return value


def _json(value, maximum=128 * 1024):
    try:
        digest = canonical_metadata(value, max_bytes=maximum, max_depth=24,
                                    max_nodes=12000, safe_integers=True)['sha256']
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')), digest
    except (NativeJSONError, ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _facts(values):
    if type(values) is dict:
        if set(values) != set(FACT_IDS) or any(values[key].get('id') != key
                for key in FACT_IDS if type(values[key]) is dict):
            _fail()
        values = [values[key] for key in FACT_IDS]
    if type(values) is not list or len(values) != len(FACT_IDS):
        _fail()
    rows = {}
    for row in values:
        if (type(row) is not dict or set(row) != _FACT_FIELDS or type(row.get('id')) is not str
                or row['id'] in rows):
            _fail()
        expected = make_fact(row['id'], label=row['label'], state=row['state'], source=row['source'],
                             observed_at=row['observed_at'], read_at=row['read_at'],
                             identity=row['identity'], data=row['data'])
        if row['evidence_sha256'] != expected['evidence_sha256']:
            _fail('evidence_conflict')
        rows[row['id']] = expected
    if set(rows) != set(FACT_IDS):
        _fail()
    result = [rows[key] for key in FACT_IDS]
    return json.loads(_json(result)[0])


def make_fact(fact_id, *, label, state, source, observed_at=None, read_at=None,
              identity=None, data=None):
    if (type(fact_id) is not str or fact_id not in FACT_IDS or type(state) is not str
            or state not in FACT_STATES):
        _fail()
    identity = {} if identity is None else identity
    data = {} if data is None else data
    if type(identity) is not dict or type(data) is not dict:
        _fail()
    value = {'id': fact_id, 'label': _text(label, 100), 'state': state,
             'source': _text(source, 256), 'observed_at': None if observed_at is None else _timestamp(observed_at),
             'identity': identity, 'data': data}
    raw, digest = _json(value, 32 * 1024)
    value = json.loads(raw)
    value.update(read_at=_timestamp(read_at) if read_at is not None else _now(), evidence_sha256=digest)
    return value


def _analysis(value, check_id, facts):
    if (type(value) is not dict or set(value) != {'check_id', 'summary', 'items'}
            or value['check_id'] != check_id or type(value['items']) is not list
            or len(value['items']) != len(FACT_IDS)):
        _fail('invalid_analysis')
    _text(value['summary'], 300, empty=True)
    by_id = {row['id']: row for row in facts}
    seen = set()
    for row in value['items']:
        if (type(row) is not dict or set(row) != {'evidence_id', 'evidence_sha256', 'judgment', 'note'}
                or type(row['evidence_id']) is not str or row['evidence_id'] not in by_id
                or row['evidence_id'] in seen or type(row['judgment']) is not str
                or row['judgment'] not in JUDGMENTS):
            _fail('invalid_analysis')
        fact = by_id[row['evidence_id']]
        if (row['evidence_sha256'] != fact['evidence_sha256']
                or fact['state'] == 'unverified' and row['judgment'] == 'consistent'
                or fact['state'] == 'skipped' and row['judgment'] not in ('not_applicable', 'unverified')):
            _fail('invalid_analysis')
        _text(row['note'], 600, empty=True)
        seen.add(row['evidence_id'])
    normalized = dict(value, items=sorted(value['items'], key=lambda row: FACT_IDS.index(row['evidence_id'])))
    return _json(normalized, 16 * 1024)


def _applicability(record, current_facts):
    if record.get('analysis_json') is None or current_facts is None:
        return record
    current = {row['id']: row['evidence_sha256'] for row in _facts(current_facts)}
    for row in record['analysis_json']['items']:
        row['applicable'] = current[row['evidence_id']] == row['evidence_sha256']
    return record


class StatusCheckStore:
    def __init__(self, path, *, workspace_id, owner_generation):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        if (type(workspace_id) is not str or _SHA.fullmatch(workspace_id) is None
                or type(owner_generation) is not str or _TOKEN.fullmatch(owner_generation) is None
                or not self.path.parent.is_dir() or self.path.is_symlink()
                or any(parent.is_symlink() for parent in self.path.parents)):
            _fail()
        self.workspace_id, self.owner_generation = workspace_id, owner_generation

    @contextmanager
    def _open(self, *, write=False):
        if not write and not self.path.exists():
            yield None
            return
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + '?mode=' + ('rwc' if write else 'ro'),
                             uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if write:
                db.execute(_DDL)
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS status_check_active_scope ON status_checks "
                           "(workspace_id,scope_sha256) WHERE stage IN ('collecting','facts_ready','analyzing')")
            elif not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='status_checks'").fetchone():
                yield None
                return
            yield db
            if write:
                db.commit()
                os.chmod(self.path, 0o600)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _record(self, row):
        if row is None:
            return None
        result = dict(row)
        if result['workspace_id'] != self.workspace_id or result['stage'] not in STAGES:
            _fail('invalid_record')
        for key in ('target_json', 'facts_json', 'analysis_json'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        if result['owner_generation'] != self.owner_generation and result['stage'] in ACTIVE:
            result.update(stage='unconfirmed', error_code='owner_generation_changed')
        return result

    def _find(self, db, check_id):
        return db.execute('SELECT * FROM status_checks WHERE workspace_id=? AND check_id=?',
                          (self.workspace_id, _id(check_id))).fetchone() if db is not None else None

    def _owned(self, db, check_id):
        row = self._find(db, check_id)
        if row is None:
            _fail('not_found')
        if row['owner_generation'] != self.owner_generation:
            _fail('owner_changed')
        return row

    def create(self, check_id, target):
        _id(check_id)
        if type(target) is not dict:
            _fail()
        raw, scope = _json(target, 16 * 1024)
        with self._open(write=True) as db:
            db.execute("UPDATE status_checks SET stage='unconfirmed',error_code='owner_generation_changed' "
                       "WHERE workspace_id=? AND owner_generation<>? AND stage IN ('collecting','facts_ready','analyzing')",
                       (self.workspace_id, self.owner_generation))
            row = self._find(db, check_id)
            if row is not None:
                if row['scope_sha256'] != scope:
                    _fail('scope_conflict')
                return dict(self._record(row), created=False)
            row = db.execute("SELECT * FROM status_checks WHERE workspace_id=? AND scope_sha256=? "
                             "AND stage IN ('collecting','facts_ready','analyzing')", (self.workspace_id, scope)).fetchone()
            if row is not None:
                return dict(self._record(row), created=False)
            db.execute('INSERT INTO status_checks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (check_id, self.workspace_id, self.owner_generation, scope, raw, 'collecting',
                        None, None, _now(), None, None))
            return dict(self._record(self._find(db, check_id)), created=True)

    def get(self, check_id, *, current_facts=None):
        _id(check_id)
        with self._open() as db:
            record = self._record(self._find(db, check_id))
        return _applicability(record, current_facts) if record is not None else None

    def latest(self, *, current_facts=None):
        with self._open() as db:
            row = db.execute('SELECT * FROM status_checks WHERE workspace_id=? ORDER BY created_at DESC,check_id DESC LIMIT 1',
                             (self.workspace_id,)).fetchone() if db is not None else None
            record = self._record(row)
        return _applicability(record, current_facts) if record is not None else None

    def set_facts(self, check_id, facts):
        values = _facts(facts)
        raw = _json(values)[0]
        with self._open(write=True) as db:
            row = self._owned(db, check_id)
            if row['facts_json'] is not None:
                if [f['evidence_sha256'] for f in json.loads(row['facts_json'])] != [f['evidence_sha256'] for f in values]:
                    _fail('facts_conflict')
            elif row['stage'] == 'collecting':
                db.execute("UPDATE status_checks SET facts_json=?,stage='facts_ready' WHERE check_id=?", (raw, check_id))
            else:
                _fail('stage_conflict')
            return self._record(self._find(db, check_id))

    def claim_analysis(self, check_id):
        with self._open(write=True) as db:
            row = self._owned(db, check_id)
            if row['stage'] != 'facts_ready':
                return False
            db.execute("UPDATE status_checks SET stage='analyzing' WHERE check_id=?", (check_id,))
            return True

    def publish(self, check_id, analysis, *, source, current_facts):
        if type(source) is str:
            _text(source, 256)
        elif type(source) is not dict or not source:
            _fail()
        _json(source, 8 * 1024)
        current = _facts(current_facts)
        with self._open(write=True) as db:
            row = self._owned(db, check_id)
            if row['facts_json'] is None:
                _fail('stage_conflict')
            facts = _facts(json.loads(row['facts_json']))
            raw, digest = _analysis(analysis, check_id, facts)
            if row['analysis_json'] is not None:
                record = self._record(row)
                if record['analysis_json']['result_sha256'] != digest:
                    _fail('analysis_conflict')
                return _applicability(record, current)
            if row['stage'] != 'analyzing':
                _fail('stage_conflict')
            result = json.loads(raw)
            result.update(source=source, result_sha256=digest)
            record = _applicability({'analysis_json': result}, current)
            result = record['analysis_json']
            partial = (any(not item['applicable'] or item['judgment'] == 'unverified' for item in result['items'])
                       or any(fact['state'] == 'unverified' for fact in facts))
            db.execute('UPDATE status_checks SET stage=?,analysis_json=?,published_at=?,error_code=NULL WHERE check_id=?',
                       ('partial' if partial else 'completed', _json(result)[0], _now(), check_id))
            return self._record(self._find(db, check_id))

    def fail(self, check_id, code):
        if type(code) is not str or _TOKEN.fullmatch(code) is None:
            _fail()
        with self._open(write=True) as db:
            row = self._owned(db, check_id)
            if row['stage'] in ACTIVE:
                db.execute("UPDATE status_checks SET stage='analysis_failed',error_code=? WHERE check_id=?", (code, check_id))
            return self._record(self._find(db, check_id))
