"""Manual four-part metadata check, using existing readers and text analysis.

No Agent creation, tool execution, memory recall, or automatic retry. The only
worker is a finite user-requested analysis, counted by the existing admission
gate. The store and text analyzer are independently callable.
"""
from copy import deepcopy
import hashlib
import json
import re
import threading
import uuid

from rabbit_specialist.status_checks import StatusCheckStore, make_fact
from rabbit_specialist import status_analysis


PATH = '/api/status-checks'
AGENTS = ('codex', 'pi', 'hermes', 'claude', 'dsh')
PROFILE_FIELDS = ('id', 'revision', 'name', 'agent_instance_id', 'device_id',
                  'model_id', 'reasoning_effort')
TARGET_FIELDS = ('agent', 'chat_id', 'profile_id', 'profile_revision',
                 'memory_profile_id', 'memory_profile_revision')


def target_fields(value):
    if type(value) is not dict or value.keys() - set(TARGET_FIELDS):
        raise ValueError('请选择当前对话或已保存配置。')
    result = {key: val for key, val in value.items() if val is not None}
    if result.get('agent') is not None and result['agent'] not in AGENTS:
        raise ValueError('状态检查的Agent无效。')
    for key in ('chat_id', 'profile_id', 'memory_profile_id'):
        if key in result:
            try:
                if type(result[key]) is not str or str(uuid.UUID(result[key])) != result[key]:
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError('状态检查需要准确的对象编号。') from None
    for key in ('profile_revision', 'memory_profile_revision'):
        if key in result and (type(result[key]) is not int or not 1 <= result[key] <= 2**53 - 1):
            raise ValueError('状态检查需要准确的配置版本。')
    for key in ('profile', 'memory_profile'):
        if (key + '_id' in result) != (key + '_revision' in result):
            raise ValueError('状态检查的配置编号与版本需要成对提供。')
    return result


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class StatusChecks:
    def __init__(self, service):
        self.service = service
        self.store = StatusCheckStore(service.store.path, workspace_id=service.workspace_id,
                                      owner_generation=service.instance_id)
        self.lock = threading.RLock()
        self.workers = {}

    def _selection(self, target):
        """References only. No transcript or native owner is opened."""
        if target.get('agent') and target.get('chat_id'):
            if target['agent'] == 'dsh':
                config = {'selection': {'configuration_unavailable': True}}
            else:
                config = self.service.native_chat_configuration(target['agent'], target['chat_id'])
            memory = self.service.native_chat_memory_configuration(target['agent'], target['chat_id'])
            return {'configuration': config['selection'], 'memory': memory['selection']}
        if target.get('chat_id'):
            return self.service.store.get_profile_selection(target['chat_id'])
        return {}

    def _refresh_catalogues(self, target, backend, sources):
        """Explicit metadata refresh; the existing APIs never open an owner."""
        outcomes = {}
        try:
            rows = self.service.native_model_connections()['connections']
        except Exception:
            rows = []
        for agent, source in sources.items():
            if not source['enabled'] or backend is not None and agent != backend:
                continue
            candidates = [row for row in rows if row['agent'] == agent
                and (not target.get('chat_id') or row['chat_id'] == target['chat_id'])]
            try:
                if candidates:
                    row = candidates[0]
                    self.service.refresh_native_model_catalog({key: row[key] for key in ('agent', 'chat_id', 'epoch')})
                elif agent == 'codex':
                    self.service.refresh_specialist_model_catalog()
                else:
                    outcomes[agent] = 'no_ready_connection'
                    continue
                outcomes[agent] = 'observed'
            except Exception:
                outcomes[agent] = 'unverified'
        return outcomes

    def collect(self, target, *, deep=True, baseline=None, refresh_catalogues=False):
        old = {row['id']: row for row in baseline or []}
        result = []
        selection = None
        try:
            selection = self._selection(target)
            saved = self.service.profiles.saved_state()['profiles']
            selected = (selection.get('configuration', selection) or {}).get('ai_profile_id')
            profile_id = selected if target.get('chat_id') else target.get('profile_id')
            if target.get('profile_id') and target.get('chat_id') and selected != target['profile_id']:
                raise ValueError('configuration_selection_changed')
            rows = ([row for row in saved if row['id'] == profile_id] if profile_id else [] if target.get('chat_id')
                    else [row for row in saved if row['agent_instance_id'] == 'local/' + target['agent']]
                    if target.get('agent') else saved)
            if profile_id and not rows:
                raise KeyError('profile_missing')
            if len(rows) > 64:
                raise ValueError('scope_capacity')
            values = [{key: row[key] for key in PROFILE_FIELDS} for row in rows]
            if ((selection.get('configuration') or {}).get('configuration_unavailable')
                    or target.get('agent') and any(row['agent_instance_id'] != 'local/' + target['agent'] for row in values)
                    or target.get('profile_id') and target.get('profile_revision')
                    and any(row['revision'] != target['profile_revision'] for row in values)):
                raise ValueError('configuration_changed')
            identity = {'profiles': [{'id': row['id'], 'revision': row['revision']} for row in values],
                        'selection': selection}
            fact = make_fact('configuration', label='AI 配置', state='observed',
                source='saved_ai_profiles', identity=identity,
                data={'profiles': values, 'scope': 'saved_parameters', 'actual_call_checked': False,
                      'native_settings_inherited': bool(target.get('agent') and target.get('chat_id') and not profile_id)})
        except Exception:
            fact = make_fact('configuration', label='AI 配置', state='unverified',
                source='saved_ai_profiles', identity={'target': target}, data={'reason': 'configuration_unreadable'})
        result.append(fact)
        try:
            sources = self.service._native_profile_capabilities()
            backend = target.get('agent')
            if backend is None and target.get('profile_id'):
                row = next(row for row in saved if row['id'] == target['profile_id'])
                backend = row['agent_instance_id'].removeprefix('local/')
            refreshed = (self._refresh_catalogues(target, backend, sources) if refresh_catalogues else
                         old.get('catalogue', {}).get('data', {}).get('metadata_refresh', {}))
            if refresh_catalogues:
                sources = self.service._native_profile_capabilities()
            entries = []
            for agent, source in sources.items():
                if backend is not None and agent != backend:
                    continue
                entries.append({'agent': agent, 'enabled': source['enabled'], 'source': source['source'],
                    'catalogue_available': source['catalogue_available'], 'observed_at': source['observed_at'],
                    'source_chat_id': source['source_chat_id'], 'models_count': len(source['models']),
                    'models_sha256': digest(source['models'])})
            ready = bool(entries) and all(row['catalogue_available'] for row in entries) and all(
                outcome == 'observed' for outcome in refreshed.values())
            checks = []
            if result[0]['state'] == 'observed':
                for profile in rows:
                    try:
                        self.service._resolve_native_profile_from_cache(profile)
                        passed = True
                    except Exception:
                        passed = False
                    checks.append({'profile_id': profile['id'], 'revision': profile['revision'],
                                   'passed': passed, 'source': 'cached_catalogue_or_inheritance'})
            fact = make_fact('catalogue', label='原生模型目录', state='observed' if ready else 'unverified',
                source='native_profile_capabilities', identity={'entries': entries},
                observed_at=entries[0]['observed_at'] if len(entries) == 1 else None,
                data={'entries': entries, 'parameter_checks': checks, 'metadata_refresh': refreshed,
                      'actual_call_checked': False})
        except Exception:
            fact = make_fact('catalogue', label='原生模型目录', state='unverified',
                source='native_profile_capabilities', identity={'agent': target.get('agent')},
                data={'reason': 'catalogue_unreadable'})
        result.append(fact)
        try:
            holder = self.service.specialist
            value = holder.observe_work() if holder is not None else {}
            identity = {'enabled': holder is not None, 'connection_state': value.get('connection_state'),
                        'thread_id': value.get('native_session_id'), 'epoch': value.get('epoch')}
            if not deep and old.get('tools', {}).get('identity') == identity:
                fact = deepcopy(old['tools'])
            elif identity['connection_state'] == 'ready' and identity['thread_id'] and identity['epoch'] and deep:
                tools = holder.tools(thread_id=identity['thread_id'], epoch=identity['epoch'])
                entries = [{'name': row['name'], 'auth_status': row.get('auth_status'),
                            'runtime_status': row.get('runtime_status'),
                            'tools_error_present': row['tools_error_present'],
                            'tool_count': len(row['tool_names'])} for row in tools['servers']]
                fact = make_fact('tools', label='专员工具连接', state='observed', source='specialist/mcpServerStatus/list',
                    identity=identity, data={'servers': entries, 'complete': tools['complete'], 'tools_executed': False})
            else:
                fact = make_fact('tools', label='专员工具连接', state='unverified', source='specialist/mcpServerStatus/list',
                    identity=identity, data={'reason': 'no_ready_connection' if deep else 'connection_changed'})
        except Exception:
            fact = make_fact('tools', label='专员工具连接', state='unverified', source='specialist/mcpServerStatus/list',
                identity={'enabled': self.service.specialist is not None}, data={'reason': 'tool_directory_unreadable'})
        result.append(fact)
        try:
            if selection is None:
                raise ValueError('selection_unreadable')
            selected = selection.get('memory', selection) or {}
            memory_id = selected.get('memory_profile_id') if target.get('chat_id') else target.get('memory_profile_id')
            if target.get('memory_profile_id') and target.get('chat_id') and memory_id != target['memory_profile_id']:
                raise ValueError('memory_selection_changed')
            if not memory_id or memory_id == 'none':
                fact = make_fact('memory', label='所选记忆', state='skipped', source='memory_profile_selection',
                    identity={'memory_profile_id': None, 'selection': selected}, data={'reason': 'no_memory_selected'})
            else:
                profile = self.service.memory_profiles.get(memory_id)
                if target.get('memory_profile_revision') and profile['revision'] != target['memory_profile_revision']:
                    raise ValueError('memory_configuration_changed')
                # The fingerprint is private identity only; never include habits,
                # memory text, credentials or endpoint URLs in the analysis.
                fingerprint = self.service._memory_fingerprint(self.service._memory_driver_for_profile(profile))
                identity = {'profile_id': memory_id, 'revision': profile['revision'],
                            'program': profile['program'], 'connection_sha256': fingerprint,
                            'selection': selected}
                if not deep and old.get('memory', {}).get('identity') == identity:
                    fact = deepcopy(old['memory'])
                elif profile['program'] == 'hermes_files':
                    fact = make_fact('memory', label='所选记忆', state='unverified', source='saved_memory_profile',
                        identity=identity, data={'reason': 'document_scan_skipped', 'profile_saved': True,
                                                'bank_state_checked': False})
                elif deep:
                    value = self.service.check_memory_connection(memory_id, profile['revision'])
                    fact = make_fact('memory', label='所选记忆', state='observed' if value['status'] == 'connected' else 'unverified',
                        source='memory_connection_status', identity=identity,
                        data={key: value[key] for key in ('status', 'write_supported', 'bank_state', 'counts')})
                else:
                    fact = make_fact('memory', label='所选记忆', state='unverified', source='memory_connection_status',
                        identity=identity, data={'reason': 'memory_configuration_changed'})
        except Exception:
            fact = make_fact('memory', label='所选记忆', state='unverified', source='memory_connection_status',
                identity={'memory_profile_id': target.get('memory_profile_id')}, data={'reason': 'memory_metadata_unreadable'})
        result.append(fact)
        return result

    def _public(self, row):
        if row is None:
            return None
        result = {key: row[key] for key in ('workspace_id', 'check_id', 'stage', 'created_at', 'published_at', 'error_code')}
        result.update(schema_version=1, target=row['target_json'], facts=row['facts_json'] or [], analysis=row['analysis_json'])
        return result

    def read(self, check_id=None):
        row = self.store.get(check_id) if check_id else self.store.latest()
        if row and row['analysis_json']:
            current = self.collect(row['target_json'], deep=False, baseline=row['facts_json'])
            row = self.store.get(row['check_id'], current_facts=current)
        return self._public(row)

    def start(self, check_id, target):
        target = target_fields(target)
        with self.lock:
            row = self.store.create(check_id, target)
            if not row['created']:
                return self._public(row)
            cancel = threading.Event()
            with self.service.admission.lease('service') as parent:
                reservation = parent.reserve('worker')
                def work():
                    try:
                        with reservation.handoff():
                            self._work(row['check_id'], target, cancel)
                    finally:
                        with self.lock:
                            self.workers.pop(row['check_id'], None)
                worker = threading.Thread(target=work, name='rabbit-status-check', daemon=True)
                self.workers[row['check_id']] = (worker, cancel)
                try:
                    worker.start()
                except BaseException:
                    reservation.cancel()
                    self.workers.pop(row['check_id'], None)
                    self.store.fail(row['check_id'], 'worker_not_started')
                    raise
            return self._public(row)

    def _work(self, check_id, target, cancel):
        try:
            facts = self.collect(target, refresh_catalogues=True)
            self.store.set_facts(check_id, facts)
            if cancel.is_set():
                raise InterruptedError()
            adapter = self.service.adapters.get('claude')
            if adapter is None:
                raise status_analysis.StatusAnalysisError('unavailable')
            route, _source = status_analysis.prepare(adapter)
            if not self.store.claim_analysis(check_id):
                return
            value, source = status_analysis.analyze(adapter, route, facts, check_id, self.service.workspace, cancel)
            # One bounded second metadata observation before publication, not
            # a second model call. Ordinary GETs never repeat these RPC reads.
            current = self.collect(target, deep=True, baseline=facts)
            if cancel.is_set():
                raise InterruptedError()
            self.store.publish(check_id, value, source=source, current_facts=current)
        except InterruptedError:
            self.store.fail(check_id, 'cancelled')
        except Exception as error:
            code = getattr(error, 'code', 'analysis_failed')
            if type(code) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}', code):
                code = 'analysis_failed'
            self.store.fail(check_id, code)

    def stop(self, *, wait=False):
        with self.lock:
            workers = list(self.workers.values())
            for _worker, cancel in workers:
                cancel.set()
        if wait:
            for worker, _cancel in workers:
                worker.join(timeout=5)
            if any(worker.is_alive() for worker, _cancel in workers):
                raise RuntimeError('状态检查仍在收尾，请稍后再维护。')
