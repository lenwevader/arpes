"""Top level module for PyARPES."""

# pylint: disable=unused-import
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._typing import ConfigSettings, ConfigType
# Use both version conventions for people's sanity.
VERSION = "4.0.1"
__version__ = VERSION


__all__ = ["__version__"]


SOURCE_ROOT = str(Path(__file__).parent)
DATA_PATH: str | None = None
HAS_LOADED: bool = False

if not HAS_LOADED:
    import arpes.config
