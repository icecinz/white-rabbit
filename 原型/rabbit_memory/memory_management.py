"""Saved memory configuration only; no health, Bank, recall or provisioning.

The supplied stores own the existing private chat database. Configuration ACKs
do not assert a physical Bank exists. A copy reserves a fresh empty namespace,
not a copy of remembered content or of a native source binding.
"""
from .memory_profiles import MUTABLE_FIELDS, make_memory_snapshot, validate_memory_profile_id
from .native_memory_bindings import NativeMemoryBindings


SCOPE = 'saved_memory_parameters'
_BASE = {'scope': SCOPE, 'connection_checked': False, 'inference_performed': False}
_CREATE_FIELDS = MUTABLE_FIELDS + ('program', 'connection_id')


def _fields(body, required=(), optional=()):
    if (type(body) is not dict or not set(required) <= body.keys()
            or body.keys() - set(required) - set(optional)):
        raise ValueError('记忆配置操作的字段不在允许清单内。')
    return dict(body)


def _owned_profile(profile):
    return dict(make_memory_snapshot(profile), locked_mode=False, source_kind='owned')


def saved_memory_profile(profile, bindings):
    """Public saved parameters and immutable binding kind, without service I/O."""
    linked = bindings.get(profile['id']) is not None
    return dict(make_memory_snapshot(profile), locked_mode=linked,
                source_kind='hermes_hindsight_readonly' if linked else 'owned')


def saved_memory_profile_in(db, profile):
    """Public parameters and provenance in one existing caller transaction."""
    linked = NativeMemoryBindings.get_in(db, profile['id']) is not None
    return dict(make_memory_snapshot(profile), locked_mode=linked,
                source_kind='hermes_hindsight_readonly' if linked else 'owned')


def saved_memory_profiles(profiles, bindings):
    return dict(_BASE, memory_profiles=[saved_memory_profile(profile, bindings)
                                      for profile in profiles.list()['profiles']])


def memory_profile_creation(profiles, bindings, creation_id):
    # create/copy never create a native binding. Preserve the original ACK's
    # kind instead of deriving it from any later state of the result profile.
    receipt = profiles.get_creation_receipt(creation_id)
    if receipt is not None:
        receipt = dict(receipt, profile=_owned_profile(receipt['profile']))
    return dict(_BASE, receipt=receipt)


def apply_memory_profile_operation(profiles, bindings, operation):
    if type(operation) is not dict:
        raise ValueError('记忆配置操作需要明确的操作类型。')
    action = operation.get('action')
    if action == 'create':
        value = _fields(operation, ('action', 'creation_id', 'profile'))
        fields = _fields(value['profile'], ('name',), _CREATE_FIELDS)
        profile = profiles.create(**fields, creation_id=validate_memory_profile_id(value['creation_id']))
        public = _owned_profile(profile)
    elif action in ('update', 'copy'):
        value = _fields(operation, ('action', 'profile_id', 'revision', 'changes') +
                        (('creation_id',) if action == 'copy' else ()))
        changes = _fields(value['changes'], (), MUTABLE_FIELDS)
        if action == 'copy':
            if changes.get('name', '') is None:
                raise ValueError('复制名称需要明确文字，或省略以使用默认副本名称。')
            profile = profiles.copy(value['profile_id'], value['revision'], **changes,
                                    creation_id=validate_memory_profile_id(value['creation_id']))
            public = _owned_profile(profile)
        else:
            if not changes:
                raise ValueError('请选择至少一项要修改的记忆配置参数。')
            linked = bindings.get(value['profile_id']) is not None
            if linked and changes.get('mode', 'read_only') != 'read_only':
                raise ValueError('原有Hermes记忆固定只读取，原绑定不变。')
            profile = profiles.update(value['profile_id'], value['revision'], **changes)
            public = _owned_profile(profile)
            if linked:
                public.update(locked_mode=True, source_kind='hermes_hindsight_readonly')
    else:
        raise ValueError('目前只支持新建、编辑或复制记忆配置。')
    return dict(_BASE, action=action, profile=public)
