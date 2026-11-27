"""Independent open-source tools for xBloom Studio machines.

Read-only telemetry, explicitly opted-in controls, pluggable byte-link
connectors, offline recipe authoring and encoding, and a local recipe and
brew history store. Bluetooth support needs the optional ``ble`` extra.
"""

from .client import ProtocolCommandError, ReadOnlyClient, SessionError
from .coffee import encode_coffee_recipe
from .connector import Connector, normalize_identity
from .control import (
    BusyError,
    EncodedRecipe,
    IdleStateRequiredError,
    InsufficientWaterError,
    Interaction,
    Operation,
    UnsupportedStageError,
)
from .events import decode_event
from .grind_profiles import grind_profile
from .intents import Studio, capabilities, plan
from .machine_controls import (
    EncodedTeaRecipe,
    PresetModeRestoreError,
    PresetWriteError,
    SavedPreset,
    UntestedOperationError,
)
from .recipe_validation import RecipeValidationError, check_recipe, recipe_constraints
from .simulator import ScriptedConnector, SimulatedStudio
from .tea import encode_tea_recipe
from .tea_guidance import recipe_tea_references
from .tea_validation import validate_tea_recipe
from .templates import recipe_templates
from .transport import BleakConnector, CommandHooks, discover

__all__ = [
    "BleakConnector",
    "BusyError",
    "CommandHooks",
    "Connector",
    "EncodedRecipe",
    "EncodedTeaRecipe",
    "IdleStateRequiredError",
    "InsufficientWaterError",
    "Interaction",
    "Operation",
    "PresetModeRestoreError",
    "PresetWriteError",
    "ProtocolCommandError",
    "ReadOnlyClient",
    "RecipeValidationError",
    "SavedPreset",
    "ScriptedConnector",
    "SessionError",
    "SimulatedStudio",
    "Studio",
    "UnsupportedStageError",
    "UntestedOperationError",
    "capabilities",
    "check_recipe",
    "decode_event",
    "discover",
    "encode_coffee_recipe",
    "encode_tea_recipe",
    "grind_profile",
    "normalize_identity",
    "plan",
    "recipe_constraints",
    "recipe_tea_references",
    "recipe_templates",
    "validate_tea_recipe",
]
