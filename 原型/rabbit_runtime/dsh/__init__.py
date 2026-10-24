"""DSH's original CLI carrier and conservative, identity-bound receipt store.

The embedding host owns the event consumer, material preparation and UI.
Importing this subpackage does not start DSH or discover its installation.
"""
from .connection import DshNativeConnection, validate_session_id
from .rpc import (
    DshRpc, DshRpcError, DshTransportError,
    DshSubmittedTimeout, DshKnownUnsentTimeout, ProtocolError,
)
from .session_store import DSHNativeStore, DSHStoreError

__all__ = (
    'DshNativeConnection', 'validate_session_id', 'DshRpc', 'DshRpcError',
    'DshTransportError', 'DshSubmittedTimeout', 'DshKnownUnsentTimeout',
    'DSHNativeStore', 'DSHStoreError', 'ProtocolError',
)
