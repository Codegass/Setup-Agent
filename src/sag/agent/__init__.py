"""Agent package for Setup-Agent."""

from .agent import SetupAgent
from .agent_state_evaluator import AgentStateAnalysis, AgentStateEvaluator, AgentStatus
from .context_manager import (  # BranchContext is DEPRECATED
    BranchContext,
    ContextManager,
    TrunkContext,
)
from .react_engine import ReActEngine

# Note: BranchContext is deprecated and replaced by BranchContextHistory
__all__ = [
    "SetupAgent",
    "ReActEngine",
    "ContextManager",
    "TrunkContext",
    "BranchContext",
    "AgentStateEvaluator",
    "AgentStateAnalysis",
    "AgentStatus",
]
