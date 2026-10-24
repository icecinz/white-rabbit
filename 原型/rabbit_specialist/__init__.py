"""Explicit specialist role setup and reported tasks over the native runtime.

The host supplies its management tool script, document reader and admission.
Import computes expected schemas in memory; it does not discover user paths,
open disk stores, start an Agent or grant application/desktop authority.
"""
from .specialist_connection import (
    CodexSpecialistConnection, SERVER_NAME, SPECIALIST_MODEL, SPECIALIST_EFFORT, TOOLS,
)
from .specialist_workspace import prepare_workspace, SpecialistWorkspaceError
from .interface_tasks import InterfaceTasks, InterfaceTaskError, SEMANTICS, CLOSURE_SEMANTICS
from .interface_task_coordinator import InterfaceTaskCoordinator, InterfaceTaskCoordinatorError

__all__ = (
    'CodexSpecialistConnection', 'SERVER_NAME', 'SPECIALIST_MODEL', 'SPECIALIST_EFFORT',
    'TOOLS', 'prepare_workspace', 'SpecialistWorkspaceError', 'InterfaceTasks',
    'InterfaceTaskError', 'SEMANTICS', 'CLOSURE_SEMANTICS', 'InterfaceTaskCoordinator',
    'InterfaceTaskCoordinatorError',
)
