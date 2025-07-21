"""Agent package for Setup-Agent."""

from .agent import SetupAgent
from .context_manager import BranchContext, ContextManager, TrunkContext  # BranchContext is DEPRECATED
from .react_engine import ReActEngine

# Note: BranchContext is deprecated and replaced by BranchContextHistory
__all__ = ["SetupAgent", "ReActEngine", "ContextManager", "TrunkContext", "BranchContext"]
