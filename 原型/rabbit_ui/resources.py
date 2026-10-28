"""Read fixed public assets; HTTP policy and business APIs belong to the host."""
import base64
from dataclasses import dataclass
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import stat


INLINE_STYLES = ('style.css', 'workspace.css', 'specialist.css', 'work_status.css')
INLINE_SCRIPTS = (
    'agents.js', 'markdown-it.min.js', 'rich_text.js', 'materials.js',
    'status_checks.js', 'profiles.js', 'native_memory.js', 'work_status.js', 'workspace.js', 'browser_state.js',
    'text_file_download.js', 'file_tasks.js', 'file_materials.js',
    'project_publish.js', 'markdown_chats.js', 'conversation_groups.js',
    'history_quotes.js', 'native_materials.js', 'handoff_view.js',
    'specialist_context.js', 'specialist_tools.js', 'specialist.js', 'pi_native_history.js',
    'pi_native.js', 'hermes_native_history.js', 'hermes_attachment_state.js',
    'hermes_native.js', 'claude_material_draft.js', 'claude_native.js',
    'dsh_material_draft.js', 'dsh_native.js', 'native_chats.js',
    'native_handoff.js', 'native_adoption.js', 'native_text_quote.js',
    'native_file_quote.js', 'app.js',
)
HELP_FILES = ('manual.md', 'specialist-guide.md')
INLINE_ASSETS = frozenset(('index.html', 'rabbit-app-1024.png') + INLINE_STYLES + INLINE_SCRIPTS + HELP_FILES)
ASSET_FILES = ('index.html', 'rabbit-app-1024.png', 'app.webmanifest',
               'markdown-it.LICENSE') + INLINE_STYLES + INLINE_SCRIPTS + HELP_FILES
MAX_INLINE_ASSET_BYTES = 2 * 1024 * 1024
_ROUTES = {'/': 'index.html', **{'/' + name: name for name in ASSET_FILES if name != 'markdown-it.LICENSE'}}
_CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.png': 'image/png',
    '.webmanifest': 'application/manifest+json; charset=utf-8',
    '.md': 'text/markdown; charset=utf-8',
}


@dataclass(frozen=True)
class Resource:
    data: bytes
    content_type: str
    nonce: str | None = None


class ResourceNotFound(LookupError):
    """The route is not one of this package's exact public resource URLs."""


def _bootstrap(value):
    if type(value) is not str:
        raise ValueError('助手身份脚本必须由宿主明确提供。')
    if len(value.encode('utf-8')) > MAX_INLINE_ASSET_BYTES:
        raise ValueError('助手身份脚本超过大小限制。')
    return value


def _root(web_root):
    root = Path(web_root)
    if root.is_symlink():
        raise ValueError('界面素材目录无效。')
    return root.resolve(strict=True)


def _asset(root, name, *, text=False, inline=False):
    if name not in (INLINE_ASSETS if inline else ASSET_FILES):
        raise ValueError('界面素材不在固定清单内。')
    path = root / name
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError('界面素材必须为普通文件。')
    # A fixed leaf must remain a regular bounded file when actually opened.
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('界面素材必须为普通文件。')
        if info.st_size > MAX_INLINE_ASSET_BYTES:
            raise ValueError('界面素材超过大小限制。')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(MAX_INLINE_ASSET_BYTES + 1)
        if len(raw) > MAX_INLINE_ASSET_BYTES:
            raise ValueError('界面素材超过大小限制。')
        if len(raw) != info.st_size:
            raise ValueError('界面素材未能完整读取。')
    finally:
        os.close(descriptor)
    return raw.decode('utf-8') if text else raw


def self_contained_document(web_root, *, agent_bootstrap):
    """Bundle only fixed public assets, preserving their exact UTF-8 source.

    The authenticated preview can load its initial HTML by an access ticket,
    while cross-site native frames cannot reuse Strict cookies for CSS/JS.
    Base64 keeps HTML rewriting away from source strings and prevents literal
    closing tags in CSS/JS from cutting off the document. The browser creates
    nonce-bearing style/script nodes; no eval or relaxed source policy is used.
    """
    root = Path(web_root)
    if root.is_symlink():
        raise ValueError('界面素材目录无效。')
    root = root.resolve(strict=True)

    def asset(name, text=False):
        return _asset(root, name, text=text, inline=True)

    html, styles, scripts = asset('index.html', text=True), [], []

    def attributes(tag):
        return {name.lower(): value for name, _quote, value in
                re.findall(r'([\w-]+)\s*=\s*([\"\'])(.*?)\2', tag, flags=re.S)}

    def link(match):
        tag, attrs = match.group(0), attributes(match.group(0))
        if attrs.get('rel') == 'stylesheet':
            name = attrs.get('href', '').removeprefix('/')
            if name not in INLINE_STYLES or attrs.get('href') != '/' + name:
                raise ValueError('界面样式不在固定清单内。')
            styles.append(name)
            return ''
        if attrs.get('rel') == 'manifest':
            return ''  # A proxied preview is not a standalone PWA installation.
        if attrs.get('href') == '/rabbit-app-1024.png':
            # Reuse the single inline brand image after the DOM is complete.
            return re.sub(r'\s+href\s*=\s*([\"\'])/rabbit-app-1024\.png\1', '', tag)
        return tag

    def script(match):
        attrs = attributes(match.group(1))
        name = attrs.get('src', '').removeprefix('/')
        if name not in INLINE_SCRIPTS or attrs.get('src') != '/' + name or attrs.get('type') not in (None, 'text/javascript'):
            raise ValueError('界面脚本不在固定清单内。')
        scripts.append(name)
        return ''

    html = re.sub(r'<link\b[^>]*>', link, html, flags=re.I)
    html = re.sub(r'<script\b([^>]*)>\s*</script\s*>', script, html, flags=re.I)
    if (len(styles) != len(INLINE_STYLES) or set(styles) != set(INLINE_STYLES)
            or len(scripts) != len(INLINE_SCRIPTS) or set(scripts) != set(INLINE_SCRIPTS)
            or re.search(r'<script\b', html, flags=re.I)):
        raise ValueError('界面素材顺序或清单不完整。')
    logo = asset('rabbit-app-1024.png')
    if not logo.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('界面图标格式无效。')
    logo_uri = 'data:image/png;base64,' + base64.b64encode(logo).decode('ascii')
    html, logo_count = re.subn(r'(<img\b[^>]*\bsrc\s*=\s*)([\"\'])/rabbit-app-1024\.png\2',
        lambda match: match.group(1) + match.group(2) + logo_uri + match.group(2), html, flags=re.I)
    if logo_count != 1:
        raise ValueError('界面图标入口不完整。')

    def encoded(names):
        return json.dumps([base64.b64encode(((agent_bootstrap if name == 'agents.js' else '')
                                            + asset(name, text=True)).encode('utf-8')).decode('ascii') for name in names])

    nonce = secrets.token_urlsafe(24)
    decode = "function decode(value){return new TextDecoder().decode(Uint8Array.from(atob(value),function(c){return c.charCodeAt(0);}));}"
    style_bootstrap = ('<script nonce="' + nonce + '">(function(){var nonce=document.currentScript.nonce;' + decode +
        'var sources=' + encoded(styles) + ';sources.forEach(function(source){var node=document.createElement("style");'
        'node.nonce=nonce;node.textContent=decode(source);document.head.appendChild(node);});})();</script>')
    script_bootstrap = ('<script nonce="' + nonce + '">(function(){var nonce=document.currentScript.nonce;' + decode +
        'var sources=' + encoded(scripts) + ';sources.forEach(function(source){var node=document.createElement("script");'
        'node.nonce=nonce;node.textContent=decode(source);document.body.appendChild(node);});'
        'var logo=document.querySelector(".brand-mark");if(logo){document.querySelectorAll("link[rel=icon],link[rel=apple-touch-icon]")'
        '.forEach(function(node){node.href=logo.src;});}})();</script>')
    if html.lower().count('</head>') != 1 or html.lower().count('</body>') != 1:
        raise ValueError('界面结构不完整。')
    html = re.sub(r'</head>', lambda _match: style_bootstrap + '</head>', html, count=1, flags=re.I)
    if re.search(r'\bid\s*=\s*([\"\'])rabbit-help-data\1', html, flags=re.I):
        raise ValueError('界面帮助入口重复。')
    help_texts = {'manual': asset('manual.md', text=True),
                  'guide': asset('specialist-guide.md', text=True)}
    if any(not value.strip() for value in help_texts.values()):
        raise ValueError('界面帮助正文为空。')
    help_data = json.dumps(help_texts, ensure_ascii=False)
    help_template = '<template id="rabbit-help-data">' + escape(help_data, quote=False) + '</template>'
    html = re.sub(r'</body>', lambda _match: help_template + script_bootstrap + '</body>', html, count=1, flags=re.I)
    return html.encode('utf-8'), nonce


def read_resource(route, *, agent_bootstrap):
    """Read an exact root URL; host code owns authentication and HTTP headers."""
    if type(route) is not str or route not in _ROUTES:
        raise ResourceNotFound('找不到这个页面。')
    agent_bootstrap = _bootstrap(agent_bootstrap)
    name = _ROUTES[route]
    root = Path(__file__).parent / 'assets'
    if name == 'index.html':
        data, nonce = self_contained_document(root, agent_bootstrap=agent_bootstrap)
    else:
        raw = _asset(_root(root), name, text=name.endswith(('.js', '.css', '.webmanifest', '.md')))
        data = ((agent_bootstrap if name == 'agents.js' else '') + raw).encode('utf-8') if type(raw) is str else raw
        nonce = None
    return Resource(data, _CONTENT_TYPES[Path(name).suffix], nonce)
