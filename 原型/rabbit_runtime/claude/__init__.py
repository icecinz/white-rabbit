"""Claude's SDK transport, exact history readers and conservative ledger.

The SDK worker is a module entry point and is deliberately not imported here.
Hosts provide the SDK interpreter, original CLI, working directory and identity.
"""
from .connection import ClaudeNativeConnection, session_uuid
from .rpc import (
    ClaudeRpc, ClaudeRpcError, ClaudeRemoteError, ClaudeTransportError,
    ClaudeSubmittedTimeout, ClaudeKnownUnsentTimeout,
)
from .session_store import ClaudeNativeStore, ClaudeStoreError, validate_uuid
from .material_protocol import ClaudeMaterialProtocolError
from .message_view import (
    ClaudeMessageViewError, project_records, original_record, original_image,
)
from .history_source import ClaudeHistorySourceError
from .session_file import verify_session_file, recheck_session_file

__all__ = (
    'ClaudeNativeConnection', 'session_uuid', 'ClaudeRpc', 'ClaudeRpcError',
    'ClaudeRemoteError', 'ClaudeTransportError', 'ClaudeSubmittedTimeout',
    'ClaudeKnownUnsentTimeout', 'ClaudeNativeStore', 'ClaudeStoreError',
    'validate_uuid', 'ClaudeMaterialProtocolError', 'ClaudeMessageViewError',
    'project_records', 'original_record', 'original_image',
    'ClaudeHistorySourceError', 'verify_session_file', 'recheck_session_file',
)
