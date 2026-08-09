"""Runtime state helpers for Setup-Agent.

Keep this package initializer dependency-free. ``docker_orch.orch`` imports
``sag.runtime.exec_env`` while its module is still being initialized; eagerly
loading the overlay here would enter the agent package and import that
half-built orchestrator again. The convenience exports remain available
lazily.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .env_overlay import (
        DEFAULT_OVERLAY_JSON,
        DEFAULT_OVERLAY_SCRIPT,
        EnvOverlayStore,
        EnvOverlayWarning,
    )

__all__ = [
    "DEFAULT_OVERLAY_JSON",
    "DEFAULT_OVERLAY_SCRIPT",
    "EnvOverlayStore",
    "EnvOverlayWarning",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".env_overlay", __name__), name)
    globals()[name] = value
    return value
