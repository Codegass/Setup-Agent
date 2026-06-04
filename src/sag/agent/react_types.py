"""Shared ReAct runtime types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

from sag.tools.base import ToolResult


class ReactModelMode(str, Enum):
    THINKING = "thinking"
    ACTION = "action"


class StepType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    SYSTEM_GUIDANCE = "system_guidance"


class ReActStep(BaseModel):
    step_type: StepType
    content: str
    tool_name: Optional[str] = None
    tool_params: Optional[Dict[str, Any]] = None
    tool_result: Optional[ToolResult] = None
    timestamp: str
    model_used: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReactModelCapabilities:
    mode: ReactModelMode
    model: str
    supports_function_calling: bool
    supports_parallel_function_calling: bool
    tool_call_format: Literal["openai", "anthropic", "prompt"]
