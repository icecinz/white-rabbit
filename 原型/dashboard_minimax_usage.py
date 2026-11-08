"""Read MiniMax cash balances and subscription quotas with their own keys."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
from secrets import token_hex
import stat
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from dashboard_usage import ReadFailure, SourceContext, _AMOUNT, _NoRedirect, _fingerprint


# MiniMax's official CLI selects these hosts and endpoints by region and key type.
_REGIONS = {
    'cn': ('中国大陆', 'https://api.minimax.cn', 'https://platform.minimax.cn', 'CNY'),
    'global': ('国际', 'https://api.minimax.io', 'https://platform.minimax.io', 'USD'),
}
_SETTINGS_FILE = 'dashboard-minimax.json'
_SETTINGS_LIMIT = 4096
_SETTINGS_LOCK = Lock()
_KEY_FIELDS = {'account_key': 'api', 'subscription_key': 'cp'}
_SETUP = '填入按量付费 Key（sk-api-）读取余额，或订阅 Key（sk-cp-）读取套餐用量。'
_AUTH_REJECTED = 'MiniMax 拒绝了当前认证，请核对密钥及其地区。'
_MODEL_LABEL = re.compile(r'[A-Za-z0-9.*_ -]{1,64}\Z')


def _validate_settings(value):
    if type(value) is not dict or value.keys() != {'region', *_KEY_FIELDS}:
        raise ValueError('请选择账户地区并填写对应的 MiniMax 密钥。')
    region = value['region']
    if not isinstance(region, str) or region not in _REGIONS:
        raise ValueError('请选择有效的 MiniMax 账户地区。')
    for field, prefix in _KEY_FIELDS.items():
        key = value[field]
        if not isinstance(key, str) or (key and re.fullmatch(rf'sk-{prefix}-[A-Za-z0-9_-]{{8,512}}', key) is None):
            raise ValueError(f'MiniMax 密钥格式有误，请使用 sk-{prefix}- 开头的密钥。')
    return value


@contextmanager
def _settings_directory(data_dir):
    descriptor = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ValueError('无法安全访问 MiniMax 的本机配置。')
        yield descriptor
    finally:
        os.close(descriptor)


def _read_settings(directory):
    try:
        descriptor = os.open(_SETTINGS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('MiniMax 的本机配置必须是仅当前用户可读写的普通文件。')
        body = stream.read(_SETTINGS_LIMIT + 1)
    if len(body) > _SETTINGS_LIMIT:
        raise ValueError('MiniMax 的本机配置超出大小限制。')
    return _validate_settings(json.loads(body))


def _configuration(settings):
    return {'region': settings['region'], 'account_configured': bool(settings['account_key']),
            'subscription_configured': bool(settings['subscription_key'])}


def save_configuration(data_dir, body):
    """Write only the newly supplied keys, returning presence and region."""
    supplied = _validate_settings(body)
    if not any(supplied[field] for field in _KEY_FIELDS):
        raise ValueError('请至少填写一种 MiniMax 密钥。')
    try:
        with _SETTINGS_LOCK, _settings_directory(data_dir) as directory:
            previous = _read_settings(directory)
            settings = dict(supplied)
            if previous and previous['region'] == settings['region']:
                for field in _KEY_FIELDS:
                    settings[field] = settings[field] or previous[field]
            temporary = '.minimax-' + token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(json.dumps(settings).encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, _SETTINGS_FILE, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except (OSError, ValueError, RecursionError):
        raise ValueError('MiniMax 的本机配置保存失败，请检查配置文件及权限。') from None
    return _configuration(settings)


def _environment_settings():
    key = os.environ.get('MINIMAX_API_KEY', '')
    return _validate_settings({
        'region': os.environ.get('MINIMAX_REGION') or 'cn',
        'account_key': key if key.startswith('sk-api-') else '',
        'subscription_key': key if not key.startswith('sk-api-') else '',
    })


def _presentation(settings):
    _, _, console, _ = _REGIONS[settings['region']]
    return {
        'configuration': _configuration(settings),
        'entry': {'label': '打开 MiniMax 控制台', 'url': console + '/console/home'},
        'detail_entries': [
            {'label': '查看余额', 'url': console + '/console/recharge-records'},
            {'label': '查看按量账单', 'url': console + '/console/consumption-detail'},
            {'label': '查看套餐用量', 'url': console + '/console/plan'},
        ],
    }


def context(data_dir):
    """Resolve local configuration only. Network reads happen on manual refresh."""
    try:
        with _SETTINGS_LOCK, _settings_directory(data_dir) as directory:
            settings = _read_settings(directory)
        if settings is None:
            settings = _environment_settings()
    except (OSError, ValueError, RecursionError):
        settings = {'region': 'cn', 'account_key': '', 'subscription_key': ''}
        return SourceContext(status='needs_configuration', presentation=_presentation(settings),
                             message='无法核对 MiniMax 的本机配置。' + _SETUP)
    presentation = _presentation(settings)
    scope = 'MiniMax ' + _REGIONS[settings['region']][0] + '平台账户'
    if not any(settings[field] for field in _KEY_FIELDS):
        return SourceContext(status='needs_configuration', message=_SETUP, scope=scope,
                             presentation=presentation)
    revision = _fingerprint('minimax-v1', *(settings[field] for field in ('region', *_KEY_FIELDS)))
    return SourceContext(revision, lambda: _read_sources(settings), scope=scope, presentation=presentation)


def _get_json(url, key):
    request = Request(url, method='GET', headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError('response too large')
        result = json.loads(raw)
        if type(result) is not dict:
            raise ValueError('invalid response')
        base = result.get('base_resp', {'status_code': 0})
        if type(base) is not dict:
            raise ValueError('invalid status')
        status = base.get('status_code')
        if status == 1004:
            raise ReadFailure(_AUTH_REJECTED, 'needs_login')
        if type(status) is not int or status != 0:
            raise ReadFailure('MiniMax 官方接口暂时未能返回数据。')
        return result
    except HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ReadFailure(_AUTH_REJECTED, 'needs_login') from None
        raise ReadFailure('MiniMax 官方接口查询失败。') from None
    except (URLError, OSError, ValueError, RecursionError):
        raise ReadFailure('MiniMax 官方接口数据暂时无法读取。') from None


def _balance_metrics(result, currency):
    rows = []
    for field, label in (('available_amount', '可用金额'), ('cash_balance', '现金余额'),
                         ('voucher_balance', '代金券余额'), ('credit_balance', '授信余额'),
                         ('owed_amount', '欠费金额')):
        value = result.get(field)
        if isinstance(value, str) and _AMOUNT.fullmatch(value):
            rows.append({'label': label, 'value': value, 'unit': currency, 'group': '按量付费余额',
                         'detail': field != 'available_amount'})
        elif field == 'available_amount':
            raise ReadFailure('MiniMax 本次未返回可显示的可用余额。')
    return rows


def _percentage(value):
    return value if type(value) in (int, float) and 0 <= value <= 100 else None


def _window_percentage(model, window):
    percent = _percentage(model.get(f'current_{window}_remaining_percent'))
    if percent is not None:
        return percent
    if model.get(f'current_{window}_status') == 2:
        return 0
    # The official CLI treats legacy count-only replies as remaining counts.
    total = model.get(f'current_{window}_total_count')
    count = model.get(f'current_{window}_usage_count')
    if type(total) is int and type(count) is int and 0 <= count <= total <= 2**63 - 1 and total > 0:
        return round(count * 100 / total, 2)
    return None


def _window_timing(model, window):
    prefix = 'weekly_' if window == 'weekly' else ''
    start, end = model.get(prefix + 'start_time'), model.get(prefix + 'end_time')
    timing = {}
    if type(end) is int and 946684800000 <= end <= 4102444800000:
        timing['resets_at'] = datetime.fromtimestamp(end / 1000, tz=timezone.utc).isoformat()
        if type(start) is int and 0 < end - start <= 31 * 24 * 60 * 60000:
            timing['window_minutes'] = round((end - start) / 60000)
    return timing


def _quota_metrics(result):
    models = result.get('model_remains')
    if type(models) is not list or len(models) > 32:
        raise ReadFailure('MiniMax 返回的套餐窗口无法识别。')
    rows = []
    ordered = sorted((model for model in models if type(model) is dict),
                     key=lambda model: model.get('model_name') != 'general')
    for model in ordered:
        name = model.get('model_name')
        if not isinstance(name, str) or _MODEL_LABEL.fullmatch(name) is None:
            continue
        if all(model.get(f'current_{window}_status') == 3 and model.get(f'current_{window}_total_count') == 0
               for window in ('interval', 'weekly')):
            continue
        for window, label in (('interval', '本期'), ('weekly', '本周')):
            percent = _window_percentage(model, window)
            group = '套餐用量 · ' + ('通用' if name == 'general' else name)
            detail = any(row['group'] != group for row in rows if not row['detail'])
            if model.get(f'current_{window}_status') == 3:
                rows.append({'label': f'{label}额度', 'value': '不限额', 'unit': '',
                             'group': group, 'detail': detail})
            elif percent is not None:
                rows.append({'label': f'{label}剩余', 'value': percent, 'unit': '%', 'group': group,
                             'detail': detail, **_window_timing(model, window)})
                rows.append({'label': f'{label}已用', 'value': round(100 - percent, 2), 'unit': '%',
                             'group': group, 'detail': True})
    if not rows:
        raise ReadFailure('MiniMax 本次未返回可显示的套餐额度。')
    return rows


def _read_sources(settings):
    _, api, _, currency = _REGIONS[settings['region']]
    metrics, failures = [], []
    for field, path, parse in (
        ('account_key', '/account/query_balance', lambda result: _balance_metrics(result, currency)),
        ('subscription_key', '/v1/token_plan/remains', _quota_metrics),
    ):
        if not settings[field]:
            continue
        try:
            metrics.extend(parse(_get_json(api + path, settings[field])))
        except ReadFailure as error:
            failures.append(error)
    if not metrics:
        raise failures[0]
    notes = [str(error) for error in failures]
    if not settings['account_key']:
        notes.append('配置按量付费 Key 后可读取现金余额。')
    if not settings['subscription_key']:
        notes.append('配置订阅 Key 后可读取套餐用量。')
    return metrics, ' '.join(dict.fromkeys(notes))
