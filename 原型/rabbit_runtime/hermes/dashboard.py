"""Open the installed Hermes dashboard after an explicit local launch action."""

from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import re
import subprocess
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.parse import urlencode

from .connection import HermesNativeConnection


_HOST = "127.0.0.1"
_PORT = 9119
_WAIT_SECONDS = 15
_PROBE_LIMIT = 256 * 1024
_ASSET = re.compile(rb'\bsrc="(/assets/[A-Za-z0-9_.-]+\.js)"')
_UI_MARKER = b"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__="


class HermesDashboardError(ValueError):
    """A display-safe reason that the local Hermes UI could not be opened."""


def _probe(asset):
    """Return 'ready', 'vacant' or 'occupied' without following redirects."""
    connection = HTTPConnection(_HOST, _PORT, timeout=0.5)
    try:
        try:
            connection.connect()
        except ConnectionRefusedError:
            return "vacant"
        connection.request("GET", "/chat")
        response = connection.getresponse()
        if response.status != 200 or response.headers.get_content_type() != "text/html":
            return "occupied"
        body = response.read(_PROBE_LIMIT)
        # Hermes injects its UI bootstrap at response time. Also match the
        # configured installation's bundled script, so a generic localhost
        # page or a headless Hermes backend cannot satisfy readiness.
        if _UI_MARKER in body and b'src="' + asset + b'"' in body:
            return "ready"
        return "occupied"
    except (HTTPException, OSError):
        return "occupied"
    finally:
        connection.close()


class HermesDashboardLauncher:
    def __init__(self, connection):
        self._connection = connection
        self._lock = Lock()
        self._child = None

    def launch(self):
        connection = self._connection
        if not isinstance(connection, HermesNativeConnection):
            raise HermesDashboardError("请先在小白兔中连接本机 Hermes。")
        dist = connection.source_root / "hermes_cli" / "web_dist"
        try:
            with (dist / "index.html").open("rb") as index:
                asset = _ASSET.search(index.read(_PROBE_LIMIT))
        except OSError:
            asset = None
        if asset is None or not (dist / asset[1].decode("ascii").lstrip("/")).is_file():
            raise HermesDashboardError("本机 Hermes 的 Web 界面尚未安装完成。")
        home = connection.home or Path(
            os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        ).expanduser()
        if not home.is_absolute() or not home.is_dir():
            raise HermesDashboardError("本机 Hermes 的资料目录不可用。")

        url = f"http://{_HOST}:{_PORT}/chat"
        if home.parent.name == "profiles":
            url += "?" + urlencode({"profile": home.name})
        elif connection.home is not None:
            url += "?profile=default"
        with self._lock:
            state = _probe(asset[1])
            if state == "ready":
                return url
            if self._child is None or self._child.poll() is not None:
                if state == "occupied":
                    raise HermesDashboardError("端口 9119 已被占用，且未核实为本机 Hermes Web 界面。")
                self._child = self._start(connection, home, dist)
            deadline = monotonic() + _WAIT_SECONDS
            while monotonic() < deadline:
                if _probe(asset[1]) == "ready":
                    return url
                if self._child.poll() is not None:
                    raise HermesDashboardError("Hermes Web 界面启动失败。请在 Hermes 中检查本机安装。")
                sleep(0.2)
            raise HermesDashboardError("Hermes Web 界面仍在启动，请稍后再点一次。")

    @staticmethod
    def _start(connection, home, dist):
        environment = dict(os.environ)
        for name in ("PYTHONHOME", "PYTHONPATH", "HERMES_DESKTOP", "HERMES_SERVE_HEADLESS",
                     "HERMES_DASHBOARD_SESSION_TOKEN"):
            environment.pop(name, None)
        # An explicit dist path disables Hermes's recovery build as well as
        # its ordinary build. Reuse the installation's existing bundle.
        environment.update(HERMES_HOME=str(home), HERMES_WEB_DIST=str(dist))
        command = [str(connection.python), "-B", "-s", "-m", "hermes_cli.main"]
        if connection.home is not None and home.parent.name != "profiles":
            command.extend(("-p", "default"))
        command.extend(("dashboard", "--host", _HOST, "--port", str(_PORT), "--no-open"))
        try:
            child = subprocess.Popen(command, cwd=connection.source_root, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            raise HermesDashboardError("无法启动已配置的本机 Hermes。") from None
        Thread(target=child.wait, name="hermes-dashboard-reaper", daemon=True).start()
        return child
