"""Optional, narrow RPC extension around the installed Hermes session setter.

Hermes still owns the agent, parser, persistence, tools, and execution loop.
This module adds one method; it never replaces a native method. Model resolution
loads native config only inside the original session profile runtime scope.
The registry lock covers lookup through the native setter and exact read-back,
so a missing session can never reach Hermes's sessionless/global fallback.
"""
from copy import deepcopy
from pathlib import PurePosixPath
import re


METHOD = 'rabbit.session.settings'
SCHEMA_VERSION = 1
REASONING_SOURCE = 'hermes_constants.parse_reasoning_effort'
MODEL_RESOLUTION_SOURCE = 'hermes_cli.model_switch.switch_model'
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_IDENTITY_FIELDS = {'session_id', 'session_key', 'cwd', 'profile_home'}
_ERRORS = {
    'invalid_arguments': (4002, 'Invalid exact-session settings request.'),
    'not_found': (4001, 'The exact native session is no longer live.'),
    'identity_changed': (4007, 'The native session identity does not match.'),
    'not_ready': (5032, 'The native session agent is not ready.'),
    'busy': (4009, 'The native session has active or pending work.'),
    'runtime_invalid': (5032, 'The native applied settings cannot be verified.'),
    'native_failed': (5001, 'The native reasoning setter did not complete.'),
    'native_result_invalid': (5001, 'The native reasoning setter result cannot be verified.'),
    'readback_mismatch': (5001, 'The native reasoning setting differs from the requested setting.'),
    'provider_unresolved': (4002, 'The native provider identity could not be resolved.'),
    'model_unresolved': (4002, 'The native model selection could not be resolved.'),
}


class _SettingsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code][1])


def _path(value):
    if (type(value) is not str or not value or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise _SettingsError('invalid_arguments')
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise _SettingsError('invalid_arguments')
    return value


def _text(value):
    if (type(value) is not str or not value or len(value) > 2048
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise _SettingsError('runtime_invalid')
    return value


def _provider(value):
    if (type(value) is not str or not value or len(value) > 256
            or value.startswith(('-', '\u2012', '\u2013', '\u2014', '\u2015'))
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise _SettingsError('invalid_arguments')
    return value


def _reasoning(value):
    """Preserve absent/empty native config; project only the understood fields."""
    if value is None:
        return None
    if type(value) is not dict:
        raise _SettingsError('runtime_invalid')
    result = {}
    if 'enabled' in value:
        if type(value['enabled']) is not bool:
            raise _SettingsError('runtime_invalid')
        result['enabled'] = value['enabled']
    if 'effort' in value:
        if value['effort'] not in EFFORTS[1:] or type(value['effort']) is not str:
            raise _SettingsError('runtime_invalid')
        result['effort'] = value['effort']
    return result


def _identity(params):
    for key in ('session_id', 'session_key'):
        if type(params.get(key)) is not str or not _SESSION.fullmatch(params[key]):
            raise _SettingsError('invalid_arguments')
    return {key: _path(params[key]) if key in ('cwd', 'profile_home') else params[key]
            for key in _IDENTITY_FIELDS}


def _live(server, identity, *, expected=None):
    """Caller owns the native registry RLock; never waits for an agent build."""
    session = server._sessions.get(identity['session_id'])
    if type(session) is not dict:
        raise _SettingsError('not_found')
    if expected is not None and session is not expected:
        raise _SettingsError('identity_changed')
    home = session.get('profile_home') or str(server._hermes_home)
    if (session.get('session_key') != identity['session_key']
            or session.get('cwd') != identity['cwd'] or home != identity['profile_home']):
        raise _SettingsError('identity_changed')
    if any(session.get(key) for key in (
            'running', 'inflight_turn', 'queued_prompt', 'queued_prompts', 'pending_model_switch',
            '_closing', '_finalized', '_auto_continue_scheduled', '_compute_host_active',
            '_client_gone_interrupt_requested', '_busy_interrupt_pending')):
        raise _SettingsError('busy')
    ready, agent = session.get('agent_ready'), session.get('agent')
    if (ready is None or not callable(getattr(ready, 'is_set', None)) or not ready.is_set()
            or agent is None or session.get('agent_error') or session.get('resume_hydrating')
            or session.get('resume_history_error')):
        raise _SettingsError('not_ready')
    return session, agent


def _snapshot(identity, agent, session):
    return {'schema_version': SCHEMA_VERSION, **identity,
            'provider': _text(getattr(agent, 'provider', None)),
            'model': _text(getattr(agent, 'model', None)),
            'model_pinned': bool(session.get('model_override')),
            'reasoning_config': _reasoning(getattr(agent, 'reasoning_config', None))}


def _error(rid, code, *, changed=False, native_code=None):
    number, message = _ERRORS[code]
    if type(native_code) is int and -2147483648 <= native_code <= 2147483647:
        number = native_code
    return {'jsonrpc': '2.0', 'id': rid, 'error': {
        'code': number, 'message': message,
        'data': {'rabbit_code': code, 'settings_may_have_changed': changed}}}


def install_session_settings(server, *, installed_commit, expected_commit, parser=None, provider_tools=None,
                             model_tools=None):
    """Register once after the native CLI has initialized its profile environment.

    ``installed_commit`` must come from the bootstrap's local Git HEAD verifier.
    The caller-supplied pin is compared here again; it is never a claimed HEAD.
    ``parser`` is injectable for offline fixtures; production uses Hermes's parser.
    """
    if (type(installed_commit) is not str or not _COMMIT.fullmatch(installed_commit)
            or type(expected_commit) is not str or installed_commit != expected_commit):
        raise ValueError('Native Hermes commit did not match the explicit startup pin.')
    required = ('_sessions', '_sessions_lock', '_hermes_home', '_set_reasoning',
                '_session_profile_runtime_scope', '_methods', 'method')
    if any(not hasattr(server, name) for name in required) or type(server._sessions) is not dict:
        raise ValueError('The native Hermes session settings interface is unavailable.')
    if METHOD in server._methods:
        raise ValueError('The session settings RPC is already registered.')
    if parser is None:
        from hermes_constants import parse_reasoning_effort
        parser = parse_reasoning_effort
    if not all(callable(value) for value in (parser, server.method, server._set_reasoning,
                                             server._session_profile_runtime_scope)):
        raise ValueError('The native Hermes session settings interface is unavailable.')
    if provider_tools is not None and (type(provider_tools) is not tuple or len(provider_tools) != 3
            or not all(callable(value) for value in provider_tools)):
        raise ValueError('The native Hermes provider resolution interface is unavailable.')
    if model_tools is not None and (type(model_tools) is not tuple or len(model_tools) != 4
            or not all(callable(value) for value in model_tools)):
        raise ValueError('The native Hermes model resolution interface is unavailable.')
    capabilities = {
        'schema_version': SCHEMA_VERSION, 'method': METHOD, 'guard': 'exact_live_session',
        'reasoning_source': REASONING_SOURCE, 'installed_commit': installed_commit,
        'actions': ['read', 'set_reasoning', 'resolve_provider', 'resolve_model'], 'provider_resolution': True,
        'model_resolution': True, 'model_resolution_source': MODEL_RESOLUTION_SOURCE,
    }

    def handler(rid, params):
        changed = False
        try:
            if type(params) is not dict:
                raise _SettingsError('invalid_arguments')
            action = params.get('action')
            if action == 'capabilities':
                if set(params) != {'action'}:
                    raise _SettingsError('invalid_arguments')
                return {'jsonrpc': '2.0', 'id': rid, 'result': deepcopy(capabilities)}
            if action not in ('read', 'set_reasoning', 'resolve_provider', 'resolve_model'):
                raise _SettingsError('invalid_arguments')
            expected_fields = {'action', *_IDENTITY_FIELDS}
            if action == 'set_reasoning':
                expected_fields.add('reasoning_effort')
            elif action in ('resolve_provider', 'resolve_model'):
                expected_fields.add('provider')
                if action == 'resolve_model':
                    expected_fields.add('model')
            if set(params) != expected_fields:
                raise _SettingsError('invalid_arguments')
            identity = _identity(params)
            effort, wanted = None, None
            provider = _provider(params.get('provider')) if action in ('resolve_provider', 'resolve_model') else None
            if action == 'resolve_model':
                model = params['model']
                if (type(model) is not str or not model or model != model.strip() or len(model) > 1024
                        or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in model)):
                    raise _SettingsError('invalid_arguments')
            if action == 'set_reasoning':
                effort = params.get('reasoning_effort')
                if type(effort) is not str or effort not in EFFORTS:
                    raise _SettingsError('invalid_arguments')
                wanted = parser(effort)
                if (_reasoning(wanted) != ({'enabled': False} if effort == 'none'
                        else {'enabled': True, 'effort': effort})):
                    raise _SettingsError('runtime_invalid')
            # This is the native registry lock, not an app-side lock around two RPCs.
            with server._sessions_lock:
                session, agent = _live(server, identity)
                if action == 'resolve_model':
                    if model_tools is None:
                        from hermes_cli.config import load_config, get_compatible_custom_providers
                        from hermes_cli.model_switch import switch_model
                        from tui_gateway.model_switch import _current_model_runtime
                    else:
                        load_config, get_compatible_custom_providers, _current_model_runtime, switch_model = model_tools
                    before = _snapshot(identity, agent, session)
                    try:
                        with server._session_profile_runtime_scope(session):
                            current_provider, current_model, current_base_url, current_api_key = _current_model_runtime(agent, provider)
                            cfg = load_config()
                            if type(cfg) is not dict:
                                raise _SettingsError('model_unresolved')
                            resolved = switch_model(model, current_provider=current_provider, current_model=current_model,
                                current_base_url=current_base_url, current_api_key=current_api_key, is_global=False,
                                explicit_provider=provider, user_providers=cfg.get('providers'),
                                custom_providers=get_compatible_custom_providers(cfg))
                        if getattr(resolved, 'success', None) is not True or getattr(resolved, 'is_global', None) is not False:
                            raise _SettingsError('model_unresolved')
                        resolved_provider = _text(getattr(resolved, 'target_provider', None))
                        resolved_model = _text(getattr(resolved, 'new_model', None))
                        if len(resolved_provider) > 256 or len(resolved_model) > 1024:
                            raise _SettingsError('model_unresolved')
                    except Exception:
                        raise _SettingsError('model_unresolved') from None
                    _, current_agent = _live(server, identity, expected=session)
                    if current_agent is not agent or _snapshot(identity, agent, session) != before:
                        raise _SettingsError('identity_changed')
                    return {'jsonrpc': '2.0', 'id': rid, 'result': {
                        'schema_version': SCHEMA_VERSION, **identity, 'requested_provider': provider,
                        'requested_model': model, 'resolved_provider': resolved_provider, 'resolved_model': resolved_model}}
                if action == 'resolve_provider':
                    if provider_tools is None:
                        from hermes_cli.config import load_config, get_compatible_custom_providers
                        from hermes_cli.providers import resolve_provider_full
                    else:
                        load_config, get_compatible_custom_providers, resolve_provider_full = provider_tools
                    with server._session_profile_runtime_scope(session):
                        cfg = load_config()
                        if type(cfg) is not dict:
                            raise _SettingsError('provider_unresolved')
                        definition = resolve_provider_full(provider, cfg.get('providers'),
                            get_compatible_custom_providers(cfg))
                    if definition is None:
                        raise _SettingsError('provider_unresolved')
                    _, current_agent = _live(server, identity, expected=session)
                    if current_agent is not agent:
                        raise _SettingsError('identity_changed')
                    return {'jsonrpc': '2.0', 'id': rid, 'result': {
                        'schema_version': SCHEMA_VERSION, **identity,
                        'requested_provider': provider, 'resolved_provider': _text(getattr(definition, 'id', None))}}
                _snapshot(identity, agent, session)  # validate before any native mutation
                if action == 'set_reasoning':
                    changed = True
                    try:
                        with server._session_profile_runtime_scope(session):
                            reply = server._set_reasoning(rid, {
                                'session_id': identity['session_id'], 'scope': 'session'},
                                'reasoning', effort, session)
                    except Exception:
                        return _error(rid, 'native_failed', changed=True)
                    if type(reply) is not dict:
                        raise _SettingsError('native_result_invalid')
                    if 'error' in reply:
                        remote = reply.get('error')
                        code = remote.get('code') if type(remote) is dict else None
                        return _error(rid, 'native_failed', changed=True, native_code=code)
                    result = reply.get('result')
                    if (reply.get('id') != rid or reply.get('jsonrpc') != '2.0' or type(result) is not dict
                            or result.get('key') != 'reasoning' or result.get('value') != effort):
                        raise _SettingsError('native_result_invalid')
                _, current_agent = _live(server, identity, expected=session)
                if current_agent is not agent:
                    raise _SettingsError('identity_changed')
                observed = _snapshot(identity, current_agent, session)
                if action == 'set_reasoning' and observed['reasoning_config'] != wanted:
                    raise _SettingsError('readback_mismatch')
                return {'jsonrpc': '2.0', 'id': rid, 'result': observed}
        except _SettingsError as exc:
            return _error(rid, exc.code, changed=changed)
        except Exception:
            return _error(rid, 'runtime_invalid', changed=changed)

    server.method(METHOD)(handler)
    if server._methods.get(METHOD) is not handler:
        raise ValueError('The native RPC registrar did not retain the settings handler.')
    return deepcopy(capabilities)
