"""Hermes's native gateway carrier, exact codecs and conservative ledger.

Hosts provide the existing Node/Python/source installation and private scope.
The direct bootstrap remains self-contained; importing this API starts nothing.
"""
from .connection import HermesNativeConnection
from .rpc import (
    HermesRpc, HermesRpcError, HermesRemoteError, HermesTransportError,
    HermesSubmittedTimeout, HermesKnownUnsentTimeout,
)
from .session_store import HermesNativeStore, HermesStoreError, validate_session_id
from .profile_catalog import (
    HermesProfileUnavailable, project_hermes_catalogue,
    resolve_hermes_profile, validate_runtime_options,
)
from .message_view import (
    HermesMessageViewError, canonical_metadata, project_messages,
    original_message, original_image, reference_value,
)
from .native_bootstrap import native_revision

__all__ = (
    'HermesNativeConnection', 'HermesRpc', 'HermesRpcError', 'HermesRemoteError',
    'HermesTransportError', 'HermesSubmittedTimeout', 'HermesKnownUnsentTimeout',
    'HermesNativeStore', 'HermesStoreError', 'validate_session_id',
    'HermesProfileUnavailable', 'project_hermes_catalogue', 'resolve_hermes_profile',
    'validate_runtime_options', 'HermesMessageViewError', 'canonical_metadata',
    'project_messages', 'original_message', 'original_image', 'reference_value',
    'native_revision',
)
