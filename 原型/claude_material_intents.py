"""Compatible Claude material identities; the shared implementation owns I/O."""
import sqlite3  # Kept as the legacy test/instrumentation import surface.

from native_material_intents import (
    NativeMaterialIntents, NativeMaterialIntentError, SCHEMA_VERSION,
    MAX_MESSAGE_BYTES, MAX_REFS_BYTES,
)


ClaudeMaterialIntentError = NativeMaterialIntentError


class ClaudeMaterialIntents(NativeMaterialIntents):
    def __init__(self, path, *, workspace_id):
        super().__init__(path, workspace_id=workspace_id, agent='claude')
