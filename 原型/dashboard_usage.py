"""Account and exact-session readings for the dashboard."""

from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Lock
import tomllib
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from hermes_adapter import _native_text, _official_base_url
from rabbit_runtime.codex_rpc import CodexRpc, CodexRpcError, RemoteError


PATH = '/api/management/usage-sources'
REFRESH_PATH = PATH + '/refresh'
CODEX = 'codex-quota'
DEEPSEEK = 'deepseek-balance'
HERMES_SESSION = 'hermes-session-usage'
PI_SESSION = 'pi-session-usage'
DSH_SESSION = 'dsh-session-usage'
MINIMAX = 'minimax-token-plan'
LAUNCH_PATH = '/api/management/agent-launch'
MINIMAX_CONFIG_PATH = PATH + '/minimax-configuration'
LAUNCH_AGENTS = frozenset(('codex', 'hermes', 'pi', 'dsh'))
SOURCE_INFO = {
    CODEX: {'title': 'Codex 额度', 'scope': '本机 Codex 的 ChatGPT 登录账户',
            'scope_kind': 'account',
            'entry': {'label': '打开 Codex', 'action': 'codex'},
            'connection_agent': 'codex'},
    DEEPSEEK: {'title': 'DeepSeek 账户余额', 'scope': 'Hermes 所配置的官方 DeepSeek Key 所属账户',
               'scope_kind': 'account',
               'entry': {'label': '打开 DeepSeek', 'url': 'https://chat.deepseek.com/'},
               'additional_entries': [{'label': '打开 DeepSeek 开放平台', 'url': 'https://platform.deepseek.com/'}],
               'detail_entries': [{'label': '查看用量账单', 'url': 'https://platform.deepseek.com/usage'}],
               'connection_agent': None},
    HERMES_SESSION: {'title': 'Hermes 会话用量', 'scope': '已关联的本机 Hermes 会话',
                     'scope_kind': 'session',
                     'entry': {'label': '启动 Hermes', 'action': 'hermes'},
                     'connection_agent': 'hermes'},
    PI_SESSION: {'title': 'Pi 会话用量', 'scope': '本机 Pi 会话的已保存分支',
                 'scope_kind': 'session',
                 'entry': {'label': '在终端打开 Pi', 'action': 'pi'},
                 'connection_agent': 'pi'},
    DSH_SESSION: {'title': 'DSH 会话用量', 'scope': '本机 DSH 会话',
                  'scope_kind': 'session',
                  'entry': {'label': '打开 DSH', 'action': 'dsh'},
                  'connection_agent': 'dsh'},
    MINIMAX: {'title': 'MiniMax 余额与用量', 'scope': 'MiniMax 开放平台账户',
              'scope_kind': 'account', 'connection_agent': None},
}
DEEPSEEK_URL = 'https://api.deepseek.com/user/balance'
CODEX_BASE_URL = 'https://chatgpt.com/backend-api/'
_SANDBOX = '/usr/bin/sandbox-exec'
_READ_ONLY_PROFILE = ('(version 1)(allow default)(deny file-write*)'
                      '(allow file-write* (subpath (param "SCRATCH")) (literal "/dev/null"))'
                      '(deny file-write* (literal (param "AUTH_LINK")))'
                      '(deny network-outbound (remote ip "localhost:*"))')
_DEEPSEEK_SETUP = '请在已连接的 Hermes 资料目录中配置官方 DeepSeek 和专用 DEEPSEEK_API_KEY。'
_AMOUNT = re.compile(r'-?[0-9]{1,24}(?:\.[0-9]{1,12})?\Z')
_BUCKET_LABEL = re.compile(r'[A-Za-z0-9 ()._+/-]{1,72}\Z')
_CODEX_USAGE_GROUP = '账户用量'
_CODEX_DAILY_GROUP = '按日 Token'
_CODEX_DAILY_MAX = 366
_CODEX_USAGE_FIELDS = (('lifetimeTokens', '账户累计 Token'), ('peakDailyTokens', '单日峰值 Token'))
_CODEX_ACTIVITY_GROUP = '账户活动'
_CODEX_ACTIVITY_FIELDS = (('longestRunningTurnSec', '最长单次运行', '秒'),
                          ('currentStreakDays', '当前连续使用', '天'),
                          ('longestStreakDays', '最长连续使用', '天'))
_ENV_NAME = re.compile(r'^\s*(?:export\s+)?(DEEPSEEK_API_KEY|DEEPSEEK_BASE_URL)\s*=')


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class ReadFailure(Exception):
    def __init__(self, message, status='failed'):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SourceContext:
    fingerprint: str | None = None
    read: Callable | None = None
    status: str = 'unread'
    message: str = '尚未手动读取。'
    scope: str | None = None
    presentation: dict | None = None


def _blocked(status, message):
    return SourceContext(status=status, message=message)


def _file_revision(path, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return str(path), None
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise OSError('Source file is not an owned regular file.')
    return (str(path), info.st_dev, info.st_ino, info.st_ctime_ns,
            info.st_mtime_ns, info.st_size)


def _fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()


def _codex_context(command):
    if not command:
        return _blocked('needs_configuration', '请先连接本机 Codex。')
    executable = shutil.which(command)
    if not executable or not os.access(_SANDBOX, os.X_OK):
        return _blocked('unavailable', '当前 Codex 或 macOS 只读启动环境不可用。')
    root = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    if not root.is_absolute():
        return _blocked('unavailable', 'Codex 的资料目录需要使用绝对路径。')
    try:
        root = root.resolve(strict=True)
        auth = root / 'auth.json'
        config = root / 'config.toml'
        settings_revision = _file_revision(config, optional=True)
        settings = tomllib.loads(_native_text(config, 256 * 1024, optional=True))
        if (settings.get('cli_auth_credentials_store', 'file') != 'file'
                or settings.get('chatgpt_base_url', CODEX_BASE_URL).rstrip('/') != CODEX_BASE_URL.rstrip('/')):
            return _blocked('unavailable', '当前只支持 Codex 本地登录文件及官方 ChatGPT 连接。')
        try:
            auth_revision = _file_revision(auth)
        except FileNotFoundError:
            return _blocked('needs_login', '请先在 Codex 登录 ChatGPT。')
        binary = Path(executable).resolve(strict=True)
        code = binary.stat()
        revision = _fingerprint('codex-file-v1', auth_revision, settings_revision,
                                str(binary), code.st_ino, code.st_mtime_ns)
        return SourceContext(revision, lambda: _read_codex(str(binary), auth, auth_revision))
    except FileNotFoundError:
        return _blocked('needs_login', '请先在 Codex 登录 ChatGPT。')
    except (OSError, ValueError, AttributeError):
        return _blocked('unavailable', '无法核对本机 Codex 的登录和连接配置。')


def _codex_metrics(result):
    if type(result) is not dict or type(result.get('rateLimits')) is not dict:
        raise ReadFailure('Codex 返回的额度格式无法识别。')
    main = result['rateLimits']
    buckets = result.get('rateLimitsByLimitId') or {}
    if type(buckets) is not dict or len(buckets) > 32:
        raise ReadFailure('Codex 返回的额度分组无法识别。')
    primary_id = main.get('limitId')
    has_main_bucket = (isinstance(primary_id, str) and primary_id in buckets) or main in buckets.values()
    rows = [] if has_main_bucket else [(primary_id, main)]
    # Show the ordinary bucket before any supplementary provider buckets.
    rows.extend(sorted(buckets.items(), key=lambda item: item[0] != primary_id))
    metrics = []
    for group_id, bucket in rows:
        if type(bucket) is not dict:
            raise ReadFailure('Codex 返回的额度分组无法识别。')
        group = next((value for value in (bucket.get('limitName'), group_id)
                      if isinstance(value, str) and _BUCKET_LABEL.fullmatch(value)), 'Codex')
        for field, label in (('primary', '主要窗口已用'), ('secondary', '次要窗口已用')):
            window = bucket.get(field)
            if window is None:
                continue
            if (type(window) is not dict or type(window.get('usedPercent')) is not int
                    or not 0 <= window['usedPercent'] <= 100):
                raise ReadFailure('Codex 返回的额度窗口无法识别。')
            metric = {'label': label, 'value': window['usedPercent'], 'unit': '%', 'group': group}
            if window.get('windowDurationMins') is not None:
                minutes = window['windowDurationMins']
                if type(minutes) is not int or not 1 <= minutes <= 10_000_000:
                    raise ReadFailure('Codex 返回的额度窗口时长无法识别。')
                metric['window_minutes'] = minutes
            if window.get('resetsAt') is not None:
                try:
                    timestamp = window['resetsAt']
                    if type(timestamp) is not int:
                        raise ValueError
                    metric['resets_at'] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    raise ReadFailure('Codex 返回的恢复时间无法识别。') from None
            metrics.append(metric)
    if not metrics:
        raise ReadFailure('Codex 本次没有返回可显示的额度窗口。', 'unavailable')
    credits = main.get('credits')
    if isinstance(credits, dict):
        balance = credits.get('balance')
        if credits.get('unlimited') is True:
            value = '不限量'
        elif isinstance(balance, str) and _AMOUNT.fullmatch(balance):
            value = balance
        elif type(credits.get('hasCredits')) is bool:
            value = '可用' if credits['hasCredits'] else '无可用积分'
        else:
            value = None
        if value is not None:
            metrics.append({'label': '额外积分', 'value': value, 'unit': '',
                            'group': '额度补充', 'detail': True})
    return metrics, ''


def _codex_usage_metrics(result):
    if not isinstance(result, dict):
        return []
    metrics = []
    summary = result.get('summary')
    if isinstance(summary, dict):
        for field, label in _CODEX_USAGE_FIELDS:
            value = summary.get(field)
            if type(value) is int and 0 <= value <= 2 ** 63 - 1:
                # Keep the native int64 exact when the browser decodes the JSON.
                metrics.append({'label': label, 'value': str(value), 'unit': 'Token',
                                'group': _CODEX_USAGE_GROUP, 'detail': True})
        for field, label, unit in _CODEX_ACTIVITY_FIELDS:
            value = summary.get(field)
            if type(value) is int and 0 <= value <= 2 ** 63 - 1:
                metrics.append({'label': label, 'value': str(value), 'unit': unit,
                                'group': _CODEX_ACTIVITY_GROUP, 'detail': True})
    days = result.get('dailyUsageBuckets')
    values = {}
    if isinstance(days, list):
        for row in days:
            if not isinstance(row, dict):
                continue
            label, value = row.get('startDate'), row.get('tokens')
            if not isinstance(label, str) or type(value) is not int or not 0 <= value <= 2 ** 63 - 1:
                continue
            try:
                if date.fromisoformat(label).isoformat() != label:
                    continue
            except ValueError:
                continue
            # Duplicate rows can be shown once. Conflicting counts are ambiguous.
            if values.setdefault(label, value) != value:
                values[label] = None
    dates = sorted((label for label, value in values.items() if value is not None), reverse=True)
    metrics.extend({'label': label, 'value': str(values[label]), 'unit': 'Token',
                    'group': _CODEX_DAILY_GROUP, 'detail': True} for label in dates[:_CODEX_DAILY_MAX])
    return metrics


def _read_codex(binary, auth, auth_revision):
    if _file_revision(auth) != auth_revision:
        raise ReadFailure('Codex 的登录账户已变化，请重新更新。')
    with tempfile.TemporaryDirectory(prefix='rabbit-quota-') as scratch:
        scratch = str(Path(scratch).resolve())
        auth_link = Path(scratch) / 'auth.json'
        auth_link.symlink_to(auth)
        command = [_SANDBOX, '-D', 'SCRATCH=' + scratch,
                   '-D', 'AUTH_LINK=' + str(auth_link), '-p', _READ_ONLY_PROFILE,
                   binary, 'app-server', '--listen', 'stdio://']
        options = {'sqlite_home': scratch, 'log_dir': scratch,
                   'cli_auth_credentials_store': 'file', 'chatgpt_base_url': CODEX_BASE_URL,
                   'analytics.enabled': False, 'check_for_update_on_startup': False,
                   'mcp_servers': {}, 'plugins': {}, 'features.plugins': False, 'features.apps': False}
        for key, value in options.items():
            command.extend(('-c', key + '=' + json.dumps(value)))
        environment = {key: os.environ[key] for key in ('PATH', 'HOME',
                       'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ') if key in os.environ}
        environment['TMPDIR'] = scratch
        # The quota sandbox denies loopback connections, including local proxies.
        environment['NO_PROXY'] = 'chatgpt.com'
        # Only this child uses a disposable native configuration directory.
        # The original login is referenced read-only and is never copied.
        environment['CODEX_HOME'] = scratch
        environment['CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED'] = '1'
        # Native auth can attempt an automatic refresh even with refreshToken
        # false. Route it to a loopback destination denied by the child sandbox.
        environment['CODEX_REFRESH_TOKEN_URL_OVERRIDE'] = 'http://127.0.0.1:1/'
        phase = 'initialize'
        try:
            with CodexRpc(command, cwd=scratch, env=environment, max_frame_bytes=1024 * 1024) as rpc:
                rpc.request('initialize', {'clientInfo': {'name': 'xiaobaitu_usage',
                    'title': '小白兔额度', 'version': '0.1.0'}}, timeout=10)
                rpc.notify('initialized', {}, timeout=2)
                phase = 'account'
                account = rpc.request('account/read', {'refreshToken': False}, timeout=5)
                identity = account.get('account') if isinstance(account, dict) else None
                if not isinstance(identity, dict) or identity.get('type') != 'chatgpt':
                    raise ReadFailure('请先在 Codex 登录 ChatGPT，再手动读取额度。', 'needs_login')
                phase = 'limits'
                result = rpc.request('account/rateLimits/read', {'excludeResetCreditDetails': True}, timeout=9)
                routing = account.get('workspaceRouting')
                expected = routing.get('chatgptAccountId') if isinstance(routing, dict) else None
                observed = result.get('accountId') if isinstance(result, dict) else None
                if expected and observed and expected != observed:
                    raise ReadFailure('Codex 返回的账户范围发生变化，请重新更新。')
                metrics, _ = _codex_metrics(result)
                try:
                    # Omit threadId so the statistics describe this authenticated account.
                    activity = rpc.request('account/usage/read', timeout=9)
                except RemoteError as error:
                    if error.code == -32601:
                        return metrics, '此版本的 Codex 尚未提供账户 Token 用量接口。'
                    return metrics, 'Codex 账户 Token 用量暂时无法读取。'
                except CodexRpcError:
                    return metrics, 'Codex 账户 Token 用量暂时无法读取。'
                activity_account = activity.get('accountId') if isinstance(activity, dict) else None
                if activity_account and (expected or observed) and activity_account != (expected or observed):
                    return metrics, 'Codex 账户 Token 用量的账户范围暂未确认。'
                token_metrics = _codex_usage_metrics(activity)
                message = ''
                if not any(row['unit'] == 'Token' for row in token_metrics):
                    message = '本次未读到 Codex 账户 Token 用量。'
                elif sum(row['group'] == _CODEX_USAGE_GROUP for row in token_metrics) < len(_CODEX_USAGE_FIELDS):
                    message = '本次只读到部分 Codex 账户 Token 用量。'
                return metrics + token_metrics, message
        except RemoteError as error:
            if error.code == -32601:
                raise ReadFailure('此版本的 Codex 尚未提供本机额度读取接口。', 'unavailable') from None
            if phase == 'account':
                raise ReadFailure('Codex 未能核对当前账户或工作区，请在 Codex 查看账户状态。') from None
            raise ReadFailure('Codex 未能读取当前账户额度，请在 Codex 查看用量。') from None
        except CodexRpcError:
            raise ReadFailure('Codex 只读连接未完成，请在原应用核对额度。') from None


def _deepseek_context(home):
    if home is None:
        return _blocked('needs_configuration', _DEEPSEEK_SETUP)
    config, dotenv = home / 'config.yaml', home / '.env'
    try:
        import yaml
        from dotenv import dotenv_values
    except ImportError:
        return _blocked('unavailable', '本机服务缺少读取 Hermes 配置所需的 YAML 或 dotenv 依赖。')
    try:
        revision = _fingerprint('hermes-deepseek-v1', _file_revision(config), _file_revision(dotenv))
        settings = yaml.safe_load(_native_text(config, 512 * 1024))
        model = settings.get('model') if isinstance(settings, dict) else None
        if not isinstance(model, dict) or model.get('provider') != 'deepseek':
            return _blocked('needs_configuration', _DEEPSEEK_SETUP)
        provider = (settings.get('providers') or {}).get('deepseek') or {}
        custom = settings.get('custom_providers') or []
        if (type(provider) is not dict or type(custom) is not list
                or any(isinstance(row, dict) and str(row.get('name', '')).casefold() == 'deepseek' for row in custom)
                or any(value not in (None, '', 'DEEPSEEK_API_KEY') for value in
                       (model.get('key_env'), model.get('api_key_env'), provider.get('key_env'), provider.get('api_key_env')))
                or model.get('api_key') or provider.get('api_key')):
            return _blocked('needs_configuration', _DEEPSEEK_SETUP)
        env_lines = [line for line in _native_text(dotenv, 1024 * 1024).splitlines() if _ENV_NAME.match(line)]
        names = [_ENV_NAME.match(line)[1] for line in env_lines]
        if len(names) != len(set(names)):
            return _blocked('needs_configuration', 'Hermes 中有重复的 DeepSeek 认证配置，请先核对。')
        env = dotenv_values(stream=io.StringIO('\n'.join(env_lines)), interpolate=False)
        origins = (model.get('base_url'), model.get('api_base'), provider.get('base_url'),
                   env.get('DEEPSEEK_BASE_URL'), os.environ.get('DEEPSEEK_BASE_URL'))
        if any(origin and not _official_base_url({'provider': 'deepseek', 'base_url': origin})
               for origin in origins):
            return _blocked('needs_configuration', '当前 DeepSeek 连接不是可核对的官方直连。')
        key = env.get('DEEPSEEK_API_KEY')
        if not isinstance(key, str) or re.fullmatch(r'sk-[A-Za-z0-9_-]{8,256}', key) is None:
            return _blocked('needs_configuration', _DEEPSEEK_SETUP)
        # A local edit between validation and the captured revision can't bind
        # a newly resolved credential to an earlier snapshot.
        if revision != _fingerprint('hermes-deepseek-v1', _file_revision(config), _file_revision(dotenv)):
            return _blocked('unavailable', 'Hermes 的 DeepSeek 配置正在变化，请稍后更新。')
        return SourceContext(revision, lambda: _read_deepseek(key))
    except FileNotFoundError:
        return _blocked('needs_configuration', _DEEPSEEK_SETUP)
    except (OSError, AttributeError, TypeError, ValueError, yaml.YAMLError):
        return _blocked('unavailable', '无法核对 Hermes 的官方 DeepSeek 配置。')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _deepseek_metrics(result):
    if type(result) is not dict or type(result.get('is_available')) is not bool:
        raise ReadFailure('DeepSeek 返回的余额格式无法识别。')
    rows = result.get('balance_infos')
    if type(rows) is not list or not 1 <= len(rows) <= 2:
        raise ReadFailure('DeepSeek 本次没有返回可显示的余额。')
    metrics, currencies = [], set()
    for balance in rows:
        currency = balance.get('currency') if isinstance(balance, dict) else None
        if currency not in ('CNY', 'USD') or currency in currencies:
            raise ReadFailure('DeepSeek 返回的余额币种无法识别。')
        currencies.add(currency)
        for field, label in (('total_balance', '总余额'), ('granted_balance', '赠送余额'),
                             ('topped_up_balance', '充值余额')):
            amount = balance.get(field)
            if not isinstance(amount, str) or not _AMOUNT.fullmatch(amount):
                raise ReadFailure('DeepSeek 返回的余额金额无法识别。')
            metrics.append({'label': label, 'value': amount, 'unit': currency, 'group': currency,
                            'detail': field != 'total_balance'})
    return metrics, ('当前余额足以发起 API 调用。' if result['is_available']
                     else '官方接口报告当前余额不足以发起 API 调用。')


def _read_deepseek(key):
    request = Request(DEEPSEEK_URL, method='GET', headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ReadFailure('DeepSeek 返回的余额数据超出大小限制。')
        return _deepseek_metrics(json.loads(raw))
    except HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ReadFailure('DeepSeek 官方接口拒绝了当前认证，请核对 Hermes 的专用 Key。', 'needs_login') from None
        raise ReadFailure('DeepSeek 官方余额查询失败。') from None
    except (URLError, OSError, ValueError, RecursionError):
        raise ReadFailure('DeepSeek 官方余额暂时无法读取。') from None


class UsageSources:
    """Persist at most one private result per source and share simultaneous reads."""

    def __init__(self, data_dir, *, codex_command=None, hermes_home=None):
        self.path = Path(data_dir) / 'dashboard-usage.json'
        self._sources = {CODEX: lambda: _codex_context(codex_command),
                         DEEPSEEK: lambda: _deepseek_context(hermes_home)}
        self._lock = Lock()
        self._flights = {}

    @classmethod
    def from_connections(cls, data_dir, codex, specialist, hermes, *, hermes_manager=None,
                         pi_history=None, pi_manager=None, dsh_history=None, dsh_manager=None,
                         allow_minimax=True):
        import dashboard_dsh_usage
        import dashboard_hermes_usage
        import dashboard_minimax_usage
        import dashboard_pi_usage
        from rabbit_runtime.codex_connection import CodexNativeConnection
        from rabbit_runtime.hermes.connection import HermesNativeConnection
        from rabbit_specialist.specialist_connection import CodexSpecialistConnection

        command = None
        for candidate in (codex, specialist):
            if isinstance(candidate, (CodexNativeConnection, CodexSpecialistConnection)):
                command = candidate.codex_command
                break
        home = None
        if isinstance(hermes, HermesNativeConnection):
            home = hermes.home or Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
            if not home.is_absolute():
                home = None
        service = cls(data_dir, codex_command=command, hermes_home=home)
        service._sources.update({
            HERMES_SESSION: lambda: dashboard_hermes_usage.context(hermes_manager),
            PI_SESSION: lambda: dashboard_pi_usage.context(pi_history, pi_manager),
            DSH_SESSION: lambda: dashboard_dsh_usage.context(dsh_history, dsh_manager),
        })
        if allow_minimax:
            service._sources[MINIMAX] = lambda: dashboard_minimax_usage.context(data_dir)
        return service

    def _read_cache(self):
        try:
            value = json.loads(_native_text(self.path, 128 * 1024, optional=True) or '{}')
            if type(value) is dict:
                return {key: record for key, record in value.items()
                        if key in SOURCE_INFO and type(record) is dict}
        except (OSError, ValueError, RecursionError):
            pass
        return {}

    def _save_cache(self, cache):
        raw = json.dumps(cache, ensure_ascii=False).encode('utf-8')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.usage-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _card(self, source, context, cache):
        record = cache.get(source, {})
        if not context.fingerprint or context.fingerprint != record.get('fingerprint'):
            record = {}
        return {'id': source, **deepcopy(SOURCE_INFO[source]), **deepcopy(context.presentation or {}),
                'can_refresh': context.read is not None,
                'scope': context.scope or SOURCE_INFO[source]['scope'],
                'status': record.get('status', context.status), 'message': record.get('message', context.message),
                'last_success_at': record.get('last_success_at'), 'last_attempt_at': record.get('last_attempt_at'),
                'last_attempt_status': record.get('last_attempt_status'), 'metrics': record.get('metrics', [])}

    def snapshot(self):
        with self._lock:
            cache = self._read_cache()
            return [self._card(source, context(), cache) for source, context in self._sources.items()]

    def refresh(self, source):
        if not isinstance(source, str) or source not in self._sources:
            raise ValueError('请选择一个已知额度来源。')
        with self._lock:
            context = self._sources[source]()
            if context.read is None:
                return self._card(source, context, self._read_cache())
            flight_key = (source, context.fingerprint)
            future = self._flights.get(flight_key)
            owner = future is None
            if owner:
                future = self._flights[flight_key] = Future()
        if owner:
            try:
                try:
                    metrics, message = context.read()
                    status = 'ready'
                except ReadFailure as error:
                    metrics, message, status = None, str(error), error.status
                except (OSError, ValueError, RecursionError):
                    metrics, message, status = None, '来源读取未完成。', 'failed'
                with self._lock:
                    current = self._sources[source]()
                    if current.fingerprint == context.fingerprint:
                        cache = self._read_cache()
                        previous = cache.get(source, {})
                        record = dict(previous) if previous.get('fingerprint') == context.fingerprint else {}
                        attempt = _timestamp()
                        if source == CODEX and status == 'ready' and message:
                            labels = {label for _, label in _CODEX_USAGE_FIELDS}
                            activities = {label for _, label, _ in _CODEX_ACTIVITY_FIELDS}
                            present = {(row.get('group'), row['label']) for row in metrics}
                            has_daily = any(group == _CODEX_DAILY_GROUP for group, _ in present)
                            previous_metrics = record.get('metrics')
                            retained = []
                            for row in previous_metrics if isinstance(previous_metrics, list) else ():
                                if not isinstance(row, dict) or not isinstance(row.get('label'), str):
                                    continue
                                group, label = row.get('group'), row['label']
                                if not ((group == _CODEX_USAGE_GROUP and label in labels)
                                        or (group == _CODEX_ACTIVITY_GROUP and label in activities)
                                        or (group == _CODEX_DAILY_GROUP and not has_daily)):
                                    continue
                                if (group, label) in present:
                                    continue
                                read_at = row.get('read_at') or record.get('last_success_at')
                                if isinstance(read_at, str) and read_at:
                                    retained.append(dict(row, read_at=read_at))
                                    present.add((group, label))
                            if retained:
                                metrics = metrics + retained
                                message += '缺失项保留上次读数。'
                        record.update(fingerprint=context.fingerprint, status=status, message=message,
                                      last_attempt_at=attempt, last_attempt_status='ok' if status == 'ready' else 'failed')
                        if status == 'ready':
                            record.update(metrics=metrics, last_success_at=attempt)
                        cache[source] = record
                        self._save_cache(cache)
            except BaseException as error:
                future.set_exception(error)
                raise
            else:
                future.set_result(None)
            finally:
                with self._lock:
                    self._flights.pop(flight_key)
        else:
            future.result()
        with self._lock:
            return self._card(source, self._sources[source](), self._read_cache())
