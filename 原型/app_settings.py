"""Human/AI settings share the existing Preferences document and its lock.

Only five presentation options are exposed. This catalogue is descriptive data,
not an instruction channel, Agent router, credential editor or second store.
"""
import copy
import json

from preferences import PreferencesConflict, preference_option


PATH = '/api/management/settings'
MAX_BODY = 8192
FIELDS = (
    ('theme', '主题', '跟随系统、浅色或深色。', 'human_and_ai', 'dark'),
    ('density', '布局密度', '舒适留白或紧凑排列。', 'human_and_ai', 'compact'),
    ('send_shortcut', '发送快捷键', 'Enter发送，或Ctrl/⌘+Enter发送；输入法确认不发送。', 'human_and_ai', 'ctrl-enter'),
    ('markdown', 'Markdown排版', '是否将回答显示为Markdown；只影响显示，不修改原文。', 'ai_only', False),
    ('show_internal', '显示内部记录', '只显示已接入文字索引的内部记录，不扩展读取原生工具或推理正文。', 'ai_only', True),
)
KEYS = frozenset(row[0] for row in FIELDS)


class SettingsConflict(ValueError):
    def __init__(self, current):
        self.current_settings = current
        super().__init__('设置已由其他入口更新；请读取当前版本后再修改。')


def catalogue():
    return {'schema_version': 1, 'read_tool': 'rabbit_settings_read',
            'update_tool': 'rabbit_settings_update',
            'fields': [dict(key=key, label=label, description=description, surface=surface,
                example={'expected_revision': '0', 'patch': {key: example}}, **preference_option(key))
                for key, label, description, surface, example in FIELDS]}


def project(value):
    return {'scope': 'application_preferences', 'inference_performed': False,
            'revision': value['revision'], 'settings': {row[0]: copy.deepcopy(value[row[0]]) for row in FIELDS},
            'catalogue': catalogue()}


def read(preferences):
    return project(preferences.read())


def update(preferences, body):
    if type(body) is not dict or body.keys() != {'expected_revision', 'patch'}:
        raise ValueError('设置修改只接受当前版本和修改项。')
    patch = body['patch']
    if type(patch) is not dict or not patch or patch.keys() - KEYS:
        raise ValueError('只支持目录中的五个设置，且需要至少一项修改。')
    try:
        return project(preferences.update(patch, expected_revision=body['expected_revision']))
    except PreferencesConflict as error:
        raise SettingsConflict(project(error.current)) from None


def decode_body(raw):
    """Reject ambiguous HTTP input before it reaches the existing JSON writer."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate')
            value[key] = item
        return value

    def invalid(_value):
        raise ValueError('nonfinite')

    try:
        if len(raw) > MAX_BODY:
            raise ValueError('oversized')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=invalid)
        if type(value) is not dict:
            raise ValueError('object required')
        if '_one_ai_csrf' in value and type(value['_one_ai_csrf']) is not str:
            raise ValueError('invalid nonce')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('设置修改需要有界、无重复字段的JSON对象。') from None
