"""Agent package for Setup-Agent.

Low-level evidence modules live under this package. Importing the complete
SetupAgent graph whenever one of them is requested creates order-dependent
cycles, so the historical convenience exports are resolved lazily.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import SetupAgent
    from .context_manager import BranchContext, ContextManager, TrunkContext
    from .react_engine import ReActEngine

# Note: BranchContext is deprecated and replaced by BranchContextHistory
__all__ = [
    "SetupAgent",
    "ReActEngine",
    "ContextManager",
    "TrunkContext",
    "BranchContext",
]

_EXPORT_MODULES = {
    "SetupAgent": ".agent",
    "ReActEngine": ".react_engine",
    "ContextManager": ".context_manager",
    "TrunkContext": ".context_manager",
    "BranchContext": ".context_manager",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
