"""Bounded executor for the immutable memory outbox; no model dependencies.

One tick submits queued work once, or polls its original operation. Uncertain
submissions never return to the submit queue. The caller supplies the local
driver and its current connection fingerprint; no bank is provisioned here.

Call recover(service_lock_held=True) only while the application's exclusive
service lock proves the previous process is gone, before starting its worker.
This executor cannot establish ownership of that application lock itself.
"""
import datetime as dt
from contextlib import nullcontext
import inspect
import math
import re
import threading
import time
import uuid

from .hindsight_memory import MemoryServiceError
from .write_jobs import ERROR_CODES, MemoryJobConflict, MAX_REVISION, public_state


MAX_COUNT = (1 << 63) - 1


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _interval(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError('记忆轮询间隔需要有效的正数。')
    return float(value)


def _error_code(error, default='memory_service_unavailable'):
    # Exception messages, native task errors and text never enter HTTP state.
    code = error.code if isinstance(error, MemoryServiceError) else None
    return code if code in ERROR_CODES else default


def _receipt(row):
    """Shared projection for a single or batch query of private receipt rows."""
    if (not _count(row['fact_count']) or type(row['document_count']) is not int
            or row['document_count'] != 1 or not isinstance(row['verified_at'], str)
            or len(row['verified_at']) > 64):
        raise ValueError('记忆保存的文档回执无法核验。')
    try:
        stamp = dt.datetime.fromisoformat(row['verified_at'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('记忆保存的核验时间无效。') from None
    if stamp.tzinfo is None:
        raise ValueError('记忆保存的核验时间无效。')
    return {key: row[key] for key in ('fact_count', 'document_count', 'verified_at')}


class MemoryWriteRunner:
    """A service thread may call tick(); this class never starts a thread.

    driver_getter() and fingerprint_getter() must describe the same connection.
    When fingerprint_getter accepts a driver argument, its fingerprint is also
    checked against that captured driver, even if the current config goes A/B/A.
    Legacy zero-argument getters are supported for isolated synthetic drivers.
    A completed operation additionally requires document_receipt(profile,
    document_id) -> {document_count: 1, fact_count: nonnegative integer}; fresh
    bank statistics are consistency evidence, never this turn's fact count.
    """

    def __init__(self, jobs, driver_getter, fingerprint_getter, *, poll_interval=15.0,
                 unknown_poll_interval=60.0, clock=time.monotonic, admission=None):
        self.admission = admission
        if not all(callable(value) for value in (driver_getter, fingerprint_getter, clock)):
            raise ValueError('记忆执行器需要有效的本机连接读取方法。')
        self.jobs = jobs
        self.driver_getter, self.fingerprint_getter = driver_getter, fingerprint_getter
        try:
            fingerprint_signature = inspect.signature(fingerprint_getter)
            fingerprint_signature.bind()
        except (ValueError, TypeError):
            raise ValueError('记忆连接指纹读取方法需要支持当前连接检查。') from None
        try:
            fingerprint_signature.bind(object())
            self._fingerprint_accepts_driver = True
        except TypeError:
            self._fingerprint_accepts_driver = False
        self.poll_interval = _interval(poll_interval)
        self.unknown_poll_interval = _interval(unknown_poll_interval)
        self.clock = clock
        self.execution_lock = threading.Lock()
        self._started = False
        self._schedule = {}
        with jobs.lock, jobs._open() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS memory_write_receipts (
                run_id TEXT PRIMARY KEY REFERENCES memory_write_jobs(run_id),
                job_revision INTEGER NOT NULL CHECK(job_revision>0),
                fact_count INTEGER NOT NULL CHECK(fact_count>=0),
                document_count INTEGER NOT NULL CHECK(document_count=1),
                verified_at TEXT NOT NULL)''')

    def recover(self, *, service_lock_held=False):
        """Release only dead-worker claims, never infer a timeout is worker death."""
        if service_lock_held is not True:
            raise ValueError('恢复保存任务前需要持有服务独占锁并确认旧进程已停止。')
        with self.execution_lock:
            if self._started:
                raise ValueError('记忆执行器已开始工作，不能把仍在工作的任务当作旧任务恢复。')
            recovered = []
            for job in self.jobs.list()['jobs']:
                if job['claim_token'] is None:
                    continue
                try:
                    changed = self.jobs.recover_claim(job['run_id'], job['revision'], job['claim_token'])
                except MemoryJobConflict:
                    continue
                recovered.append(self._project(changed))
            return {'recovered': len(recovered), 'states': recovered}

    @staticmethod
    def _project(job, receipt=None):
        value = dict(public_state(job), run_id=job['run_id'], revision=job['revision'])
        if receipt is not None:
            value['receipt'] = receipt
        return value

    def get_receipt(self, run_id):
        """Expose only counts verified for this run's document and final revision."""
        with self.jobs.lock, self.jobs._open(readonly=True) as db:
            job = self.jobs._get_in(db, run_id)
            row = db.execute('SELECT * FROM memory_write_receipts WHERE run_id=?', (run_id,)).fetchone()
            if job['state'] != 'completed' or row is None or row['job_revision'] != job['revision']:
                return None
            return _receipt(row)

    def receipts_for_chat(self, chat_id):
        """One read-only query, keyed by the completed assistant message ID."""
        if not isinstance(chat_id, str) or len(chat_id) != 36:
            raise ValueError('聊天编号需要标准 UUID。')
        try:
            canonical = str(uuid.UUID(chat_id))
        except ValueError:
            raise ValueError('聊天编号需要标准 UUID。') from None
        if chat_id != canonical:
            raise ValueError('聊天编号需要标准小写 UUID。')
        with self.jobs.lock, self.jobs._open(readonly=True) as db:
            rows = db.execute('''SELECT r.message_id,receipt.fact_count,receipt.document_count,receipt.verified_at
                FROM memory_write_receipts AS receipt
                JOIN memory_write_jobs AS job ON job.run_id=receipt.run_id
                JOIN runs AS r ON r.id=job.run_id
                WHERE r.chat_id=? AND r.status='completed' AND job.state='completed'
                    AND receipt.job_revision=job.revision''', (chat_id,))
            result = {}
            for row in rows:
                message_id = row['message_id']
                if not isinstance(message_id, str) or not 1 <= len(message_id) <= 256 or '\x00' in message_id:
                    raise ValueError('记忆保存回执的回答编号无法核验。')
                if message_id in result:
                    raise ValueError('记忆保存回执的回答编号出现重复。')
                result[message_id] = _receipt(row)
            return result

    def get_state(self, run_id):
        job = self.jobs.get(run_id)
        # A concurrent poll may complete between these reads. Do not attach a
        # later revision's completed counts to an earlier processing snapshot.
        receipt = self.get_receipt(run_id) if job['state'] == 'completed' else None
        return self._project(job, receipt)

    def _finish(self, job, receipt):
        """Commit counts and completed state atomically, with the original claim."""
        with self.jobs.lock, self.jobs._open() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.jobs._get_in(db, job['run_id'])
            if (current['claim_token'] != job['claim_token'] or current['revision'] != job['revision']
                    or current['state'] not in ('processing', 'unknown')):
                raise MemoryJobConflict(current)
            if current['revision'] == MAX_REVISION:
                raise ValueError('这项记忆保存任务已达到版本上限。')
            final_revision = current['revision'] + 1
            db.execute('''UPDATE memory_write_jobs SET state='completed',revision=?,error_code=NULL,
                claim_token=NULL,claim_action=NULL,updated_at=?
                WHERE run_id=? AND claim_token=? AND revision=?''',
                (final_revision, receipt['verified_at'], job['run_id'], job['claim_token'], job['revision']))
            db.execute('''INSERT INTO memory_write_receipts(run_id,job_revision,fact_count,document_count,verified_at)
                VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                job_revision=excluded.job_revision,fact_count=excluded.fact_count,
                document_count=excluded.document_count,verified_at=excluded.verified_at''',
                (job['run_id'], final_revision, receipt['fact_count'], receipt['document_count'], receipt['verified_at']))
            return self.jobs._get_in(db, job['run_id'])

    def _driver(self, job):
        fingerprint = self.fingerprint_getter()
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
            raise MemoryServiceError('memory_service_unavailable', '本机记忆连接暂时无法核验。')
        if fingerprint != job['connection_fingerprint']:
            raise MemoryServiceError('memory_connection_changed', '保存任务的原连接已改变，没有换连接重发。')
        driver = self.driver_getter()
        if self._fingerprint_accepts_driver and self.fingerprint_getter(driver) != fingerprint:
            raise MemoryServiceError('memory_connection_changed', '捕获的记忆连接与原任务不一致，没有换连接执行。')
        if self.fingerprint_getter() != fingerprint:
            raise MemoryServiceError('memory_connection_changed', '记忆连接在读取时改变，没有换连接执行。')
        return driver

    def _update(self, job, *, state, error_code=None):
        return self.jobs.update(job['run_id'], job['claim_token'], state=state, error_code=error_code)

    def _process(self, job):
        submitting = job['claim_action'] == 'submit'
        try:
            driver = self._driver(job)
        except Exception as error:
            code = _error_code(error)
            # A known changed connection before the first submit cannot have
            # retained this payload. Already submitted work remains uncertain.
            state = 'failed' if submitting and code == 'memory_connection_changed' else 'unknown'
            return self._update(job, state=state, error_code=code), True
        if submitting:
            try:
                ack = driver.retain(job['profile'], job['payload'], expected_digest=job['payload_sha256'])
                if (not isinstance(ack, dict) or ack.get('state') != 'queued'
                        or ack.get('operation_id') != job['operation_id']):
                    raise MemoryServiceError('memory_write_unknown', '保存回执无法核验，未重新提交。')
            except Exception as error:
                return self._update(job, state='unknown', error_code=_error_code(error, 'memory_write_unknown')), True
            return self._update(job, state='processing'), False
        try:
            operation = driver.operation(job['profile'], job['operation_id'])
            if (not isinstance(operation, dict) or operation.get('operation_id') != job['operation_id']
                    or operation.get('state') not in ('pending', 'processing', 'completed', 'failed', 'cancelled', 'not_found')):
                raise MemoryServiceError('memory_response_invalid', '原保存任务的回执无法核验。')
            state = operation['state']
            if state in ('failed', 'cancelled'):
                return self._update(job, state='failed', error_code='memory_write_failed'), False
            if state == 'not_found':
                return self._update(job, state='unknown', error_code='memory_not_found'), True
            if state in ('pending', 'processing'):
                return self._update(job, state='processing'), False
            document = driver.document_receipt(job['profile'], job['document_id'])
            if (not isinstance(document, dict) or set(document) != {'fact_count', 'document_count'}
                    or document['document_count'] != 1 or type(document['document_count']) is not int
                    or not _count(document['fact_count'])):
                raise MemoryServiceError('memory_response_invalid', '本轮文档的保存计数无法核验。')
            bank = driver.bank_state(job['profile'], fresh=True)
            if (not isinstance(bank, dict) or bank.get('fresh') is not True
                    or bank.get('state') not in ('empty', 'ready')
                    or not all(_count(bank.get(key)) for key in ('facts', 'documents', 'pending_operations'))
                    or bank['facts'] < document['fact_count'] or bank['documents'] < 1):
                raise MemoryServiceError('memory_response_invalid', '刷新后的记忆库计数无法核验。')
            receipt = dict(document, verified_at=_now())
            return self._finish(job, receipt), False
        except MemoryJobConflict:
            raise
        except Exception as error:
            # A read error says nothing about a prior POST's success. Keep the
            # previous processing/unknown state, always querying the same ID.
            return self._update(job, state=job['state'], error_code=_error_code(error)), True

    def _schedule_next(self, job, failed):
        run_id = job['run_id']
        if job['state'] in ('completed', 'failed'):
            self._schedule.pop(run_id, None)
            return
        previous = self._schedule.get(run_id, (0, 0))[1]
        failures = min(previous + 1, 8) if failed else 0
        interval = self.unknown_poll_interval if job['state'] == 'unknown' else self.poll_interval
        delay = min(interval * (2 ** max(failures - 1, 0)), max(interval, 900.0))
        self._schedule[run_id] = (self.clock() + delay, failures)

    def tick(self, *, max_jobs=1):
        """Bounded synchronous step; all service I/O occurs outside SQL locks."""
        gate = getattr(self, 'admission', None)
        with gate.lease('memory') if gate is not None else nullcontext():
            if type(max_jobs) is not int or not 1 <= max_jobs <= 16:
                raise ValueError('一次记忆保存检查需要处理 1 到 16 项任务。')
            if not self.execution_lock.acquire(blocking=False):
                return {'processed': 0, 'states': []}
            try:
                self._started = True
                states = []
                for listed in self.jobs.list()['jobs']:
                    if len(states) >= max_jobs:
                        break
                    if listed['claim_token'] is not None or listed['state'] == 'submitting':
                        continue  # Never reclaim a possibly live worker by elapsed time.
                    due = self._schedule.get(listed['run_id'], (0, 0))[0]
                    if listed['state'] != 'queued' and self.clock() < due:
                        continue
                    job = self.jobs.claim(listed['run_id'], listed['revision'],
                                          for_poll=listed['state'] != 'queued')
                    if job is None:
                        continue
                    try:
                        changed, failed = self._process(job)
                    except MemoryJobConflict:
                        # Another explicit owner updated the claim. No re-submit or
                        # unverified receipt is published by this stale worker.
                        continue
                    self._schedule_next(changed, failed)
                    receipt = self.get_receipt(changed['run_id']) if changed['state'] == 'completed' else None
                    states.append(self._project(changed, receipt))
                return {'processed': len(states), 'states': states}
            finally:
                self.execution_lock.release()
