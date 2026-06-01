"""Agent package for Setup-Agent."""

from .agent import SetupAgent
from .context_manager import BranchContext, ContextManager, TrunkContext  # BranchContext is DEPRECATED
from .react_engine import ReActEngine
from .agent_state_evaluator import AgentStateEvaluator, AgentStateAnalysis, AgentStatus

# Note: BranchContext is deprecated and replaced by BranchContextHistory
__all__ = ["SetupAgent", "ReActEngine", "ContextManager", "TrunkContext", "BranchContext", "AgentStateEvaluator", "AgentStateAnalysis", "AgentStatus"]
