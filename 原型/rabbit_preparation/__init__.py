"""Pure preparation over explicit message and material snapshots.

The host supplies content or an explicit snapshot reader. This package has no
built-in storage locations, database connection or Agent runner.
"""
from .context import build_context, ContextCapacityError
from .file_task_context import prepare as prepare_file_context
from .text_snapshots import prepare_text_file_messages, TextSnapshotReader
from .material_references import validate_material_refs, MaterialReferenceError
from rabbit_content.attachments import (
    validate_attachment_ids, validate_native_attachments,
    MAX_MESSAGE_IMAGES, MAX_HISTORY_IMAGES, MAX_NATIVE_ATTACHMENTS,
    NATIVE_SOURCE_FIELDS,
)

__all__ = (
    'build_context', 'ContextCapacityError', 'prepare_file_context',
    'validate_attachment_ids', 'validate_native_attachments',
    'MAX_MESSAGE_IMAGES', 'MAX_HISTORY_IMAGES', 'MAX_NATIVE_ATTACHMENTS',
    'NATIVE_SOURCE_FIELDS', 'prepare_text_file_messages', 'TextSnapshotReader',
    'validate_material_refs', 'MaterialReferenceError',
)

