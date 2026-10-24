"""Durable, non-atomic application of a frozen Hermes profile.

Every mutating RPC is preceded by a durable phase. Re-entering an already
started application only reads native state; it never automatically repeats a
model/reasoning setter. Prompt submission is deliberately outside this module.
"""
from copy import deepcopy
import unicodedata

from rabbit_runtime.hermes.profile_catalog import NATIVE_REASONING_EFFORTS, validate_runtime_options


IDENTITY_FIELDS = ('session_id', 'session_key', 'cwd', 'profile_home')
_FIELDS = {'schema_version', *IDENTITY_FIELDS, 'provider', 'model', 'model_pinned', 'reasoning_config'}
_TEXT_FIELDS = {'provider': 256, 'model': 1024}
_MESSAGES = {
    'settings_invalid': 'Hermes没有返回可核对的原生实际设置，原消息尚未发送。',
    'configuration_identity': 'Hermes配置准备的原生身份与当前对话不符，原消息尚未发送。',
    'configuration_unknown': 'Hermes配置可能已部分应用，仍待核对；原消息尚未发送，未重复施加设置。',
    'configuration_failed': '这次Hermes配置准备已经失败，设置可能已部分变化；原消息尚未发送，请核对配置后重新发送。',
    'configuration_confirmation': '原生Hermes要求确认这次模型选择，原消息尚未发送。',
    'configuration_inherited_changed': 'Hermes实际设置与本次冻结的沿用设置不同，配置可能已部分变化；原消息尚未发送，未自动重置。',
}


class HermesProfileApplicationError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'configuration_unknown'
        super().__init__(_MESSAGES[self.code])


def _valid_text(value, limit):
    return (type(value) is str and 1 <= len(value) <= limit and value == value.strip()
            and not any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value))


def validate_settings(value, identity):
    if (type(value) is not dict or set(value) != _FIELDS or value.get('schema_version') != 1
            or type(value['schema_version']) is not int or type(value.get('model_pinned')) is not bool
            or any(value.get(key) != identity[key] for key in IDENTITY_FIELDS)
            or any(not _valid_text(value.get(key), limit) for key, limit in _TEXT_FIELDS.items())):
        raise HermesProfileApplicationError('settings_invalid')
    config = value.get('reasoning_config')
    if config is not None and (type(config) is not dict or config.keys() - {'enabled', 'effort'}
            or 'enabled' in config and type(config['enabled']) is not bool
            or 'effort' in config and (type(config['effort']) is not str
                                      or config['effort'] not in NATIVE_REASONING_EFFORTS[1:])):
        raise HermesProfileApplicationError('settings_invalid')
    return deepcopy(value)


class HermesProfileApplication:
    def __init__(self, intents, chat_id, *, read, request):
        self.intents, self.chat_id, self.read, self.request = intents, chat_id, read, request

    @staticmethod
    def _matches(current, target, explicit_model):
        return (all(current[key] == target[key] for key in ('provider', 'model', 'reasoning_config'))
                and (not explicit_model or current['model_pinned']))

    def _advance(self, application, phase, problem=None):
        return self.intents.advance_application(application['client_id'], application['revision'], phase, problem)

    def _verified(self, application):
        if application['phase'] == 'model_started':
            application = self._advance(application, 'model_applied')
        return application if application['phase'] == 'verified' else self._advance(application, 'verified')

    def _uncertain(self, application, target, explicit_model, problem):
        # An original timeout/odd ACK can have applied the settings. Only an
        # exact guarded observation may settle it; no native setter is repeated.
        try:
            current = self.read()
        except Exception:
            current = None
        if current is not None and self._matches(current, target, explicit_model):
            return self._verified(application), current
        if application['phase'] not in ('unknown', 'verified', 'failed'):
            self._advance(application, 'unknown', problem)
        raise HermesProfileApplicationError('configuration_unknown') from None

    def apply(self, intent, identity):
        target, client = intent['native_options'], intent['client_id']
        explicit_model = bool((intent.get('ai_profile') or {}).get('model_id'))
        application = self.intents.application(client, self.chat_id)
        if application is None:
            application = self.intents.begin_application(self.chat_id, client, identity)
        elif any(application['identity'][key] != identity[key] for key in ('session_key', 'cwd', 'profile_home')):
            raise HermesProfileApplicationError('configuration_identity')
        # A reconnect may have a new runtime id for the exact same stored key.
        # Never overwrite the application's original runtime identity.
        if application['phase'] == 'failed':
            raise HermesProfileApplicationError('configuration_failed')
        try:
            current = self.read()
        except Exception:
            if application['phase'] != 'prepared':
                return self._uncertain(application, target, explicit_model, 'read_unavailable')
            raise
        if application['phase'] != 'prepared':
            if self._matches(current, target, explicit_model):
                return self._verified(application), current
            return self._uncertain(application, target, explicit_model, 'application_reentry')
        if self._matches(current, target, explicit_model):
            return self._verified(application), current
        if explicit_model and (current['provider'] != target['provider'] or current['model'] != target['model']
                               or not current['model_pinned']):
            literal = validate_runtime_options({'provider': target['selection_provider'], 'model': target['selection_model']})
            application = self._advance(application, 'model_started')
            try:
                result = self.request('config.set', {'session_id': identity['session_id'], 'key': 'model',
                    'value': literal['model'] + ' --provider ' + literal['provider'] + ' --session'})
            except Exception:
                return self._uncertain(application, target, explicit_model, 'model_result_unknown')
            if (type(result) is not dict or result.get('deferred') is True
                    or result.get('scope') != 'session' or result.get('key') != 'model'
                    or result.get('value') != target['model'] or result.get('confirm_required') is not False):
                # Confirmation means no committed switch, but a queued switch
                # may happen later; neither is interpreted as prompt acceptance.
                if type(result) is dict and result.get('confirm_required') is True and not result.get('deferred'):
                    self._advance(application, 'failed', 'native_confirmation_required')
                    raise HermesProfileApplicationError('configuration_confirmation')
                if type(result) is dict and result.get('deferred') is True:
                    return self._uncertain(application, target, explicit_model, 'model_deferred')
                self._advance(application, 'failed', 'native_model_response_invalid')
                raise HermesProfileApplicationError('configuration_unknown')
            try:
                current = self.read()
            except Exception:
                return self._uncertain(application, target, explicit_model, 'model_read_unknown')
            if (current['provider'] != target['provider'] or current['model'] != target['model']
                    or not current['model_pinned']):
                return self._uncertain(application, target, explicit_model, 'model_read_mismatch')
            application = self._advance(application, 'model_applied')
        elif current['provider'] != target['provider'] or current['model'] != target['model']:
            self._advance(application, 'unknown', 'inherited_model_changed')
            raise HermesProfileApplicationError('configuration_inherited_changed')
        if target['reasoning_effort'] is None:
            if current['reasoning_config'] != target['reasoning_config']:
                self._advance(application, 'unknown', 'inherited_reasoning_changed')
                raise HermesProfileApplicationError('configuration_inherited_changed')
        elif current['reasoning_config'] != target['reasoning_config']:
            application = self._advance(application, 'reasoning_started')
            try:
                result = self.request('rabbit.session.settings', {'action': 'set_reasoning', **identity,
                    'reasoning_effort': target['reasoning_effort']})
            except Exception:
                return self._uncertain(application, target, explicit_model, 'reasoning_result_unknown')
            try:
                validate_settings(result, identity)
            except HermesProfileApplicationError:
                self._advance(application, 'failed', 'native_reasoning_response_invalid')
                raise HermesProfileApplicationError('configuration_unknown') from None
            try:
                current = self.read()
            except Exception:
                return self._uncertain(application, target, explicit_model, 'reasoning_read_unknown')
        if not self._matches(current, target, explicit_model):
            return self._uncertain(application, target, explicit_model, 'final_read_mismatch')
        return self._verified(application), current
