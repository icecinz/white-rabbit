"""Explicit host loader for Hermes's original file store; no Agent initialization."""
from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
import threading


_IMPORT_LOCK = threading.RLock()


def load_hermes_files_core(source_root, *, dependency_dir=None):
    """Return (factory, scope) for one explicitly selected native installation.

    No home/configuration discovery or memory read occurs here. Existing modules
    from a different installation are rejected rather than silently mixed.
    """
    root = Path(source_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('Hermes 原核心需要明确的安装目录。')
    root = root.resolve(strict=True)
    dependencies = None
    if dependency_dir is not None:
        dependencies = Path(dependency_dir)
        if not dependencies.is_absolute() or dependencies.is_symlink() or not dependencies.is_dir():
            raise ValueError('Hermes 原核心依赖需要明确的安装目录。')
        dependencies = dependencies.resolve(strict=True)
    modules = ('utils', 'hermes_constants', 'tools', 'tools.memory_tool_store', 'tools.memory_tool')
    with _IMPORT_LOCK:
        for name in modules:
            module = sys.modules.get(name)
            if module is not None:
                path = getattr(module, '__file__', None)
                if path is None or not Path(path).resolve().is_relative_to(root):
                    raise ValueError('Hermes 原核心模块来源不一致。')
        previous_path = list(sys.path)
        sys.path.insert(0, str(root))
        if dependencies is not None:
            # Import only explicitly selected native dependencies. Unlike
            # site.addsitedir, this does not execute installation .pth files.
            sys.path.append(str(dependencies))
        try:
            constants = importlib.import_module('hermes_constants')
            store = importlib.import_module('tools.memory_tool_store').MemoryStore
            importlib.import_module('tools.memory_tool')
        finally:
            sys.path[:] = previous_path

    def factory():
        return store(memory_enabled=True, user_profile_enabled=True)

    @contextmanager
    def scope(home):
        token = constants.set_hermes_home_override(home)
        try:
            yield
        finally:
            constants.reset_hermes_home_override(token)

    return factory, scope
