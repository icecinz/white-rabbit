"""Small management operations over the Service's existing saved profiles.

This module owns no database, transport, Agent or credential configuration.
UI and MCP writes share these bounded operations and AIProfiles transactions.
Stored parameters do not imply online availability or native session settings.
"""
from profiles import PROFILE_COLUMNS, PROFILE_FIELDS, UNSET, validate_profile_id


def _saved_profile(profile):
    return {key: profile[key] for key in PROFILE_COLUMNS}


def saved_ai_profiles(profiles):
    """Read stored profiles without health checks or capability discovery."""
    state = profiles.saved_state()
    return {
        'ai_profiles': [_saved_profile(profile) for profile in state['profiles']],
        'default_state': dict(state['default_state']),
        'scope': 'saved_parameters',
        'connection_checked': False,
    }


def _fields(body, required=(), optional=()):
    if (type(body) is not dict or not set(required) <= body.keys()
            or body.keys() - set(required) - set(optional)):
        raise ValueError('AI配置操作的字段不在允许清单内。')
    return dict(body)


def create_ai_profile(profiles, fields, *, creation_id=None):
    fields = _fields(fields, ('name', 'agent_instance_id'), PROFILE_FIELDS)
    return profiles.create(**fields, creation_id=creation_id)


def edit_ai_profile(profiles, profile_id, revision, changes, *, copying=False, creation_id=None):
    changes = _fields(changes, (), PROFILE_FIELDS)
    if not copying and not changes:
        raise ValueError('请选择至少一项要修改的配置参数。')
    # Faithful name-only edits/copies remain possible for offline profiles.
    validator = (lambda _fields: None) if not (changes.keys() - {'name'}) else None
    if copying:
        return profiles.copy(profile_id, revision, **changes, validator=validator, creation_id=creation_id)
    return profiles.update(profile_id, revision, **changes, validator=validator)


def set_default_ai_profile(profiles, profile_id, *, revision=UNSET):
    # Return the transaction's accepted state, never a later writer's state.
    return profiles.set_default_state(profile_id, expected_revision=revision)


def ai_profile_creation(profiles, creation_id):
    receipt = profiles.get_creation_receipt(creation_id)
    return {'scope': 'saved_parameters', 'receipt': receipt}


def apply_ai_profile_operation(profiles, operation):
    """The complete bounded management command; no transport retries here."""
    if type(operation) is not dict:
        raise ValueError('AI配置操作需要明确的操作类型。')
    action = operation.get('action')
    if action == 'create':
        value = _fields(operation, ('action', 'creation_id', 'profile'))
        profile = create_ai_profile(profiles, value['profile'], creation_id=validate_profile_id(value['creation_id']))
    elif action in ('update', 'copy'):
        value = _fields(operation, ('action', 'profile_id', 'revision') +
                        (('creation_id',) if action == 'copy' else ('changes',)),
                        ('changes',) if action == 'copy' else ())
        if action == 'copy':
            validate_profile_id(value['creation_id'])
        profile = edit_ai_profile(profiles, value['profile_id'], value['revision'], value.get('changes', {}),
                                  copying=action == 'copy', creation_id=value.get('creation_id'))
    elif action == 'set_default':
        value = _fields(operation, ('action', 'profile_id', 'default_revision'))
        state = set_default_ai_profile(profiles, value['profile_id'], revision=value['default_revision'])
        return {'action': action, 'scope': 'saved_parameters', 'inference_performed': False, 'default_state': state}
    else:
        raise ValueError('目前只支持新建、编辑、复制或设置默认AI配置。')
    return {'action': action, 'scope': 'saved_parameters', 'inference_performed': False,
            'profile': _saved_profile(profile)}


def rename_ai_profile(profiles, profile_id, body):
    """Rename only, keeping ID, routing parameters and native configuration.

    A successful rename advances the existing profile revision. Already accepted
    run snapshots stay unchanged; drafts that froze an earlier revision follow
    the normal stale-selection rules. A conflict must be read, never retried
    automatically with the new revision.
    """
    if not isinstance(body, dict) or set(body) != {'revision', 'name'}:
        raise ValueError('重命名只接受配置版本和新名称。')
    profile = edit_ai_profile(profiles, profile_id, body['revision'], {'name': body['name']})
    return {
        'profile': _saved_profile(profile),
        'scope': 'saved_parameters',
        'connection_checked': False,
    }
