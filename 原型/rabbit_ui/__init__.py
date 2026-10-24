"""Fixed Rabbit UI resources over an explicit compatible HTTP host."""
from .resources import (
    ASSET_FILES, INLINE_STYLES, INLINE_SCRIPTS, INLINE_ASSETS,
    MAX_INLINE_ASSET_BYTES, Resource, ResourceNotFound,
    read_resource, self_contained_document,
)

__all__ = (
    'ASSET_FILES', 'INLINE_STYLES', 'INLINE_SCRIPTS', 'INLINE_ASSETS',
    'MAX_INLINE_ASSET_BYTES', 'Resource', 'ResourceNotFound',
    'read_resource', 'self_contained_document',
)
