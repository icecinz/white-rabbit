"""Launch installed Agent interfaces from an explicit local dashboard action."""

import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from xml.parsers.expat import ExpatError

from rabbit_runtime.process import ProcessError, _terminate


_OPEN = '/usr/bin/open'
_TERMINAL = Path('/System/Applications/Utilities/Terminal.app')
_DSH_HOST = '127.0.0.1'
_DSH_PORT = 3080
_DSH_URL = f'http://{_DSH_HOST}:{_DSH_PORT}/'
_DSH_PATCH = Path(__file__).absolute().with_name('dashboard_dsh_loopback.json')
_DSH_STARTUP_SECONDS = 12


def _open(arguments, failure):
    try:
        subprocess.run([_OPEN, *map(str, arguments)], check=True, timeout=5,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(failure) from None


def _find_app(names, identifier, *, preferred=()):
    roots = (Path('/Applications'), Path.home() / 'Applications')
    candidates = (*preferred, *(root / name for root in roots for name in names))
    for candidate in candidates:
        try:
            info = candidate / 'Contents/Info.plist'
            if info.stat().st_size > 1024 * 1024:
                continue
            with info.open('rb') as handle:
                identity = plistlib.load(handle)
            if isinstance(identity, dict) and identity.get('CFBundleIdentifier') == identifier:
                return candidate
        except (OSError, ValueError, ExpatError, plistlib.InvalidFileException):
            continue
    return None


def _agent_argv(connection):
    node = getattr(connection, 'node', None)
    script = getattr(connection, 'cli_script', None)
    if not isinstance(node, (str, Path)) or not isinstance(script, (str, Path)):
        return None
    node, script = Path(node), Path(script)
    if (not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK)
            or not script.is_absolute() or not script.is_file() or not os.access(script, os.R_OK)):
        return None
    return [str(node), str(script)]


def _open_pi(argv):
    if not _TERMINAL.is_dir():
        raise ValueError('找不到这台 Mac 的终端 App。')
    launch_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='xiaobaitu-pi-',
                                         suffix='.command', delete=False) as handle:
            launch_file = Path(handle.name)
            # Only the saved Node and CLI paths enter this fixed script.
            handle.write('#!/bin/sh\n/bin/rm -f -- "$0"\ncd "$HOME" || exit 1\nexec '
                         + shlex.join(argv) + '\n')
            os.fchmod(handle.fileno(), 0o700)
        _open(('-a', _TERMINAL, launch_file), '无法在终端启动 Pi。')
    except (OSError, ValueError):
        if launch_file is not None:
            try:
                launch_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise ValueError('无法在终端启动 Pi。') from None


def _inspect_command(arguments):
    try:
        # macOS ps obscures non-ASCII arguments in the C locale. Keep the
        # exact UTF-8 argv comparison even when the install path contains 原型.
        result = subprocess.run(arguments, capture_output=True, encoding='utf-8',
                                timeout=2, env={'LC_ALL': 'en_US.UTF-8'})
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError('暂时无法核对 DSH 网页的本机进程。') from None
    if result.stderr.strip() or result.returncode not in (0, 1):
        raise ValueError('暂时无法核对 DSH 网页的本机进程。')
    return result.stdout.splitlines()


def _dsh_listener(argv):
    rows = _inspect_command(['/usr/sbin/lsof', '-nP', f'-iTCP:{_DSH_PORT}',
                             '-sTCP:LISTEN', '-F', 'pun'])
    if not rows:
        return None
    pids = [row[1:] for row in rows if row.startswith('p')]
    users = [row[1:] for row in rows if row.startswith('u')]
    sockets = [row[1:] for row in rows if row.startswith('n')]
    if (len(pids) != 1 or re.fullmatch(r'[1-9][0-9]*', pids[0]) is None
            or users != [str(os.getuid())] or sockets != [f'{_DSH_HOST}:{_DSH_PORT}']):
        raise ValueError('DSH 网页端口已被占用，请先关闭占用它的服务。')
    commands = _inspect_command(['/bin/ps', '-ww', '-p', pids[0], '-o', 'command='])
    if not commands:
        return None  # The listener exited between checks.
    if commands != [' '.join(argv)]:
        raise ValueError('DSH 网页端口已被占用，请先关闭占用它的服务。')
    return int(pids[0])


def _dsh_ready():
    # Direct local GET only. No redirect, proxy or credentials are involved.
    connection = http.client.HTTPConnection(_DSH_HOST, _DSH_PORT, timeout=0.4)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        return (response.status == 200
                and response.getheader('Content-Type', '').startswith('text/html')
                and b'window.__DSH_BOOT__' in response.read(4096))
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class NativeAgentLauncher:
    def __init__(self, pi_connection=None, dsh_connection=None, codex_connection=None):
        self.pi_connection = pi_connection
        self.dsh_connection = dsh_connection
        self.codex_connection = codex_connection
        self._dsh_lock = threading.Lock()

    def _codex_app(self):
        command = getattr(self.codex_connection, 'codex_command', None)
        parents = Path(command).parents if isinstance(command, (str, Path)) else ()
        preferred = tuple(path for path in parents if path.is_absolute() and path.suffix == '.app')
        return _find_app(('ChatGPT.app', 'Codex.app'), 'com.openai.codex', preferred=preferred)

    def _launch_dsh_web(self):
        argv = _agent_argv(self.dsh_connection)
        if argv is None:
            raise ValueError('请先配置本机 DSH，或安装 DeepSeek Harness 桌面 App。')
        try:
            manifest = Path(self.dsh_connection.package_root) / 'package.json'
            with manifest.open('rb') as handle:
                package = json.loads(handle.read(65_536))
            verified_version = (isinstance(package, dict) and package.get('name') == '@deepseek-ai/dsh'
                                and package.get('version') == self.dsh_connection.expected_version == '0.1.0-rc.6')
        except (AttributeError, OSError, TypeError, ValueError):
            verified_version = False
        if not verified_version:
            raise ValueError('当前 DSH 命令版本的网页入口尚未核对，请从 DSH 桌面 App 打开。')
        argv += ['web', '--patch', str(_DSH_PATCH), '--host', _DSH_HOST, '--port', str(_DSH_PORT)]
        with self._dsh_lock:
            return self._ensure_dsh_web(argv)

    def _ensure_dsh_web(self, argv):
        current = _dsh_listener(argv)
        process = None
        if current is None:
            try:
                expected = [{'id': 'webserver', 'config': {'host': _DSH_HOST, 'port': _DSH_PORT}}]
                if json.loads(_DSH_PATCH.read_text(encoding='utf-8')) != expected:
                    raise ValueError('Invalid launch patch')
                environment = dict(os.environ)
                if self.dsh_connection.home is not None:
                    environment['DSH_HOME'] = str(self.dsh_connection.home)
                process = subprocess.Popen(argv, cwd=Path.home(), env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=True)
            except (OSError, ValueError):
                raise ValueError('无法启动 DSH 本机网页。') from None
        deadline = time.monotonic() + _DSH_STARTUP_SECONDS
        ready = False
        try:
            while time.monotonic() < deadline:
                if process is not None and process.poll() is not None:
                    raise ValueError('DSH 网页未能启动，请检查本机 DSH 配置。')
                listener = _dsh_listener(argv)
                if listener is not None and _dsh_ready() and _dsh_listener(argv) == listener:
                    if process is not None:
                        threading.Thread(target=process.wait, daemon=True).start()
                    ready = True
                    return _DSH_URL
                time.sleep(0.15)
            raise ValueError('等待 DSH 网页启动超时，请稍后重试。')
        finally:
            # Keep a ready server available across a Rabbit restart. A failed
            # attempt cleans up only the process group started by this click.
            if process is not None and not ready:
                try:
                    _terminate(process)
                except ProcessError:
                    raise ValueError('DSH 启动未完成，且本次进程是否已退出暂时无法核对。') from None

    def launch(self, agent):
        if sys.platform != 'darwin':
            raise ValueError('当前系统暂不支持从小白兔打开本机 Agent。')
        if agent == 'codex':
            app = self._codex_app()
            if app is None:
                raise ValueError('找不到这台 Mac 上的 Codex 桌面 App。')
            _open(('-a', app, 'codex://launch'), '无法打开 Codex 桌面 App。')
            return None
        if agent == 'pi':
            argv = _agent_argv(self.pi_connection)
            if argv is None:
                raise ValueError('请先配置本机 Pi 的命令入口。')
            _open_pi(argv)
            return None
        if agent == 'dsh':
            app = _find_app(('DeepSeek Harness.app',), 'com.deepseek.harness')
            if app is not None:
                _open(('-a', app), '无法打开 DeepSeek Harness 桌面 App。')
                return None
            return self._launch_dsh_web()
        raise ValueError('未知的 Agent 启动入口。')
