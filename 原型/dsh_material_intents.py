"""DSH text-material identities in their own fixed catalogue table."""
from native_material_intents import NativeMaterialIntents, NativeMaterialIntentError


DSHMaterialIntentError = DshMaterialIntentError = NativeMaterialIntentError


class DshMaterialIntents(NativeMaterialIntents):
    def __init__(self, path, *, workspace_id):
        super().__init__(path, workspace_id=workspace_id, agent='dsh')
