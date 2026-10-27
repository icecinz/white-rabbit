"""Trusted host assembly of optional memory cores; no model calls or provisioning."""
from pathlib import Path


def configured_programs(data_dir, *, hermes_source=None, hermes_dependencies=None):
    from rabbit_memory.hermes_files_memory import HermesFilesMemory
    from rabbit_memory.mem0_memory import Mem0Memory
    from hermes_file_memory import load_hermes_files_core

    data = Path(data_dir).absolute()
    programs = {'hermes_files': None}
    if hermes_source is not None:
        try:
            factory, scope = load_hermes_files_core(hermes_source, dependency_dir=hermes_dependencies)
            programs['hermes_files'] = HermesFilesMemory(data / 'memory-programs/hermes-files',
                core_factory=factory, core_scope=scope)
        except (ImportError, ValueError, OSError, AttributeError):
            pass  # Keep the explicit unavailable entry; never use another core.
    programs['mem0'] = Mem0Memory(
        python=data / 'optional/mem0/venv/bin/python',
        config_path=data / 'memory-programs/mem0.json',
        root=data / 'memory-programs/mem0',
        worker_path=Path(__file__).parent / 'mem0_memory_worker.py')
    return programs
