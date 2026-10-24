"""Explicit fixed-profile metadata check; never initialize or read memories.

Health/profile/stats reads may update the memory service's caches or logs.
Connected describes this metadata observation, not successful recall, retain,
model inference or permission to write. Version checks bracket service I/O.
"""
import re
from urllib.parse import parse_qs

from .hindsight_memory import MemoryServiceError
from .memory_management import saved_memory_profile
from .memory_profiles import (MAX_REVISION, MemoryProfileConflict, make_memory_snapshot,
                             validate_memory_profile_id, validate_memory_profile_revision)


SCOPE = 'memory_connection_status'
_COUNTS = ('facts', 'documents', 'pending_operations')


def query_fields(rawquery):
    """One required canonical decimal revision; the route owns profile_id."""
    if type(rawquery) is not str or len(rawquery) > 128:
        raise ValueError('记忆连接检查需要明确的配置版本。')
    try:
        fields = parse_qs(rawquery, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=1)
    except (ValueError, UnicodeError):
        raise ValueError('记忆连接检查需要明确的配置版本。') from None
    values = fields.get('revision', [])
    if set(fields) != {'revision'} or len(values) != 1 or not re.fullmatch(r'[1-9][0-9]{0,15}', values[0]):
        raise ValueError('记忆连接检查需要明确的配置版本。')
    revision = int(values[0])
    if revision > MAX_REVISION:
        raise ValueError('记忆连接检查的配置版本无效。')
    return {'revision': revision}


def _service_status(error):
    if error.code == 'memory_bank_identity_changed':
        return 'identity_conflict'
    if error.code in ('memory_connection_changed', 'memory_native_source_changed'):
        return 'source_changed'
    return 'unavailable'


def _profile_missing_status(driver, profile):
    """Observe one precise identity fact after an ambiguous bank-state 404.

    This does not retry bank_state or a write. A stats/health 404 alone cannot
    establish bank absence; only this explicit profile read may report it.
    """
    try:
        read = getattr(driver, 'bank_profile', None)
        if not callable(read):
            return 'unavailable'
        read(profile)
    except MemoryServiceError as error:
        status = _service_status(error)
        return 'missing' if status == 'unavailable' and error.status == 404 else status
    except (OSError, ValueError):
        return 'unavailable'
    return 'unavailable'


def _metadata_state(value):
    if (type(value) is not dict or type(value.get('state')) is not str
            or value['state'] not in ('empty', 'ready')
            or any(type(value.get(key)) is not int or not 0 <= value[key] <= MAX_REVISION for key in _COUNTS)
            or (value['state'] == 'empty') != (value['facts'] == value['documents'] == 0)):
        raise ValueError('记忆库元数据无法核验。')
    return value['state'], {key: value[key] for key in _COUNTS}


def check_memory_connection(profiles, bindings, driver_factory, profile_id, revision):
    """Check one saved revision, with no provisioning or content operations."""
    validate_memory_profile_id(profile_id)
    validate_memory_profile_revision(revision)
    profile = profiles.get(profile_id)
    if profile['revision'] != revision:
        raise MemoryProfileConflict(profile)
    frozen = make_memory_snapshot(profile)
    public = saved_memory_profile(profile, bindings)
    status, write_supported, bank_state, counts = 'unavailable', False, None, None
    stage = 'factory'
    try:
        driver = driver_factory(profile)
        stage = 'health'
        health = driver.health()
        if (type(health) is not dict or health.get('available') is not True
                or type(health.get('write_supported', False)) is not bool):
            raise ValueError('记忆服务元数据无法核验。')
        write_supported = health.get('write_supported', False)
        stage = 'bank_state'
        bank_state, counts = _metadata_state(driver.bank_state(profile, fresh=False))
        status = 'connected'
    except MemoryServiceError as error:
        status = _service_status(error)
        if status == 'unavailable' and stage == 'bank_state' and error.status == 404:
            status = _profile_missing_status(driver, profile)
    except (OSError, ValueError):
        status = 'unavailable'
    current = profiles.get(profile_id)
    if make_memory_snapshot(current) != frozen:
        raise MemoryProfileConflict(current)
    return {'scope': SCOPE, 'inference_performed': False, 'profile': public,
            'status': status, 'write_supported': write_supported,
            'bank_state': bank_state, 'counts': counts}
