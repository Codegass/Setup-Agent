"""Typed executable plans shared by the reasoning scheduler and ReAct engine.

The plan deliberately contains no fuzzy parameter sketches.  An action is made
available to the actor only after every prior-output reference and executable
precondition has been resolved by this module.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class PlanInvalidation(str, Enum):
    FAILURE = "failure"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    PHASE_CHANGE = "phase_change"


class PlanFaultCode(str, Enum):
    MALFORMED_PLAN = "malformed_plan"
    MALFORMED_PLACEHOLDER = "malformed_placeholder"
    MISSING_REFERENCE = "missing_reference"
    UNKNOWN_TOOL = "unknown_tool"
    UNMET_PRECONDITION = "unmet_precondition"
    PLAN_EXHAUSTED = "plan_exhausted"


class PlanFault(ValueError):
    """A scheduler-owned fault that must result in reasoning, never guessing."""

    def __init__(
        self,
        code: PlanFaultCode,
        message: str,
        *,
        step_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.step_index = step_index


def _normalize_contract_lines(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        raise ValueError("must be a string or list of strings")
    normalized = tuple(str(item).strip() for item in value)
    if any(not item for item in normalized):
        raise ValueError("entries must be nonblank strings")
    return normalized


class PlanStep(BaseModel):
    """One action whose parameters can be executed without model re-analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    exact_params: dict[str, Any]
    preconditions: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...]
    success_criteria: tuple[str, ...]

    @field_validator("tool")
    @classmethod
    def _strip_tool(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("tool must be nonblank")
        return stripped

    @field_validator("preconditions", "expected_evidence", "success_criteria", mode="before")
    @classmethod
    def _normalize_lines(cls, value: Any) -> tuple[str, ...]:
        return _normalize_contract_lines(value)

    @field_validator("expected_evidence", "success_criteria")
    @classmethod
    def _require_contract(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("an executable plan step requires a nonempty evidence contract")
        return value

    @model_validator(mode="after")
    def _require_json_params(self) -> "PlanStep":
        try:
            json.dumps(self.exact_params, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("exact_params must contain JSON values") from exc
        return self


class ExecutablePlanStep(BaseModel):
    """A placeholder-free step safe to hand to the action model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_index: int = Field(ge=0)
    tool: str
    exact_params: dict[str, Any]
    expected_evidence: tuple[str, ...]
    success_criteria: tuple[str, ...]


_PLAN_MARKER = "CURRENT_PLAN:"
_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")
_FULL_PLACEHOLDER = re.compile(
    r"^\{\{step_(?P<ordinal>[1-9][0-9]*)\.(?P<path>[A-Za-z_][A-Za-z0-9_.]*)\}\}$"
)


class CurrentPlan(BaseModel):
    """The current ordered action program produced by one reasoning call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[PlanStep, ...] = Field(min_length=1)
    invalidate_on: tuple[PlanInvalidation, ...] = (
        PlanInvalidation.FAILURE,
        PlanInvalidation.CONFLICT,
        PlanInvalidation.UNKNOWN,
        PlanInvalidation.PHASE_CHANGE,
    )

    @field_validator("invalidate_on")
    @classmethod
    def _dedupe_invalidations(
        cls, value: tuple[PlanInvalidation, ...]
    ) -> tuple[PlanInvalidation, ...]:
        if len(set(value)) != len(value):
            raise ValueError("invalidate_on entries must be unique")
        return value

    @classmethod
    def from_thinking_response(cls, response: str) -> "CurrentPlan":
        """Extract exactly one JSON plan after the mandatory marker."""
        marker_index = response.find(_PLAN_MARKER)
        if marker_index < 0:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "thinking response is missing CURRENT_PLAN",
            )
        if response.find(_PLAN_MARKER, marker_index + len(_PLAN_MARKER)) >= 0:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "thinking response contains multiple CURRENT_PLAN payloads",
            )

        payload = response[marker_index + len(_PLAN_MARKER) :].lstrip()
        fenced = payload.startswith("```")
        if fenced:
            first_newline = payload.find("\n")
            if first_newline < 0:
                raise PlanFault(PlanFaultCode.MALFORMED_PLAN, "unterminated plan code fence")
            fence_language = payload[3:first_newline].strip().lower()
            if fence_language not in {"", "json"}:
                raise PlanFault(
                    PlanFaultCode.MALFORMED_PLAN,
                    "CURRENT_PLAN code fence must contain JSON",
                )
            payload = payload[first_newline + 1 :]

        try:
            decoded, consumed = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError as exc:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                f"CURRENT_PLAN is not valid JSON: {exc.msg}",
            ) from exc

        trailing = payload[consumed:].strip()
        if fenced:
            if not trailing.startswith("```"):
                raise PlanFault(PlanFaultCode.MALFORMED_PLAN, "unterminated plan code fence")
            trailing = trailing[3:].strip()
        if trailing:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "CURRENT_PLAN has trailing content after its JSON payload",
            )

        try:
            return cls.model_validate(decoded)
        except ValidationError as exc:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                f"CURRENT_PLAN violates the executable schema: {exc}",
            ) from exc

    # Short alias used by callers that already know the response came from the
    # thinking model.
    from_response = from_thinking_response

    def resolve_step(
        self,
        step_index: int,
        *,
        prior_results: Mapping[str | int, Any],
        available_tools: Sequence[str] | set[str] | Mapping[str, Any],
    ) -> ExecutablePlanStep:
        """Resolve one zero-based step or raise a scheduler fault before acting."""
        if step_index < 0 or step_index >= len(self.steps):
            raise PlanFault(
                PlanFaultCode.PLAN_EXHAUSTED,
                "current plan is exhausted",
                step_index=step_index,
            )

        step = self.steps[step_index]
        if step.tool not in available_tools:
            raise PlanFault(
                PlanFaultCode.UNKNOWN_TOOL,
                f"plan step {step_index + 1} names unknown tool {step.tool!r}",
                step_index=step_index,
            )

        for precondition in step.preconditions:
            match = _FULL_PLACEHOLDER.fullmatch(precondition)
            if match is None:
                raise PlanFault(
                    PlanFaultCode.MALFORMED_PLACEHOLDER,
                    "executable preconditions must be a complete prior-step placeholder",
                    step_index=step_index,
                )
            value = self._resolve_reference(
                match,
                step_index=step_index,
                prior_results=prior_results,
            )
            if not value:
                raise PlanFault(
                    PlanFaultCode.UNMET_PRECONDITION,
                    f"plan step {step_index + 1} has an unmet precondition: {precondition}",
                    step_index=step_index,
                )

        resolved_params = self._resolve_value(
            step.exact_params,
            step_index=step_index,
            prior_results=prior_results,
        )
        if not isinstance(resolved_params, dict):  # defensive; exact_params is typed as a dict
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLAN,
                "resolved exact_params must remain an object",
                step_index=step_index,
            )
        return ExecutablePlanStep(
            plan_index=step_index,
            tool=step.tool,
            exact_params=resolved_params,
            expected_evidence=step.expected_evidence,
            success_criteria=step.success_criteria,
        )

    def _resolve_value(
        self,
        value: Any,
        *,
        step_index: int,
        prior_results: Mapping[str | int, Any],
    ) -> Any:
        if isinstance(value, dict):
            resolved: dict[str, Any] = {}
            for key, child in value.items():
                if "{{" in key or "}}" in key:
                    raise PlanFault(
                        PlanFaultCode.MALFORMED_PLACEHOLDER,
                        "placeholders are not allowed in parameter names",
                        step_index=step_index,
                    )
                resolved[key] = self._resolve_value(
                    child,
                    step_index=step_index,
                    prior_results=prior_results,
                )
            return resolved
        if isinstance(value, list):
            return [
                self._resolve_value(
                    child,
                    step_index=step_index,
                    prior_results=prior_results,
                )
                for child in value
            ]
        if not isinstance(value, str):
            return value

        full_match = _FULL_PLACEHOLDER.fullmatch(value)
        if full_match is not None:
            return self._resolve_reference(
                full_match,
                step_index=step_index,
                prior_results=prior_results,
            )

        matches = list(_PLACEHOLDER.finditer(value))
        if "{{" in value or "}}" in value:
            # Every brace pair must match the strict reference grammar.  This
            # catches misspellings, unclosed braces, and unsupported expressions.
            if not matches or any(
                _FULL_PLACEHOLDER.fullmatch(match.group(0)) is None for match in matches
            ):
                raise PlanFault(
                    PlanFaultCode.MALFORMED_PLACEHOLDER,
                    f"malformed placeholder in exact_params: {value!r}",
                    step_index=step_index,
                )
            residue = _PLACEHOLDER.sub("", value)
            if "{{" in residue or "}}" in residue:
                raise PlanFault(
                    PlanFaultCode.MALFORMED_PLACEHOLDER,
                    f"malformed placeholder in exact_params: {value!r}",
                    step_index=step_index,
                )

        rendered = value
        for match in matches:
            strict = _FULL_PLACEHOLDER.fullmatch(match.group(0))
            if strict is None:  # covered above; keeps the type checker honest
                raise PlanFault(
                    PlanFaultCode.MALFORMED_PLACEHOLDER,
                    f"malformed placeholder in exact_params: {value!r}",
                    step_index=step_index,
                )
            referenced = self._resolve_reference(
                strict,
                step_index=step_index,
                prior_results=prior_results,
            )
            if isinstance(referenced, (dict, list)):
                replacement = json.dumps(referenced, separators=(",", ":"), sort_keys=True)
            else:
                replacement = str(referenced)
            rendered = rendered.replace(match.group(0), replacement)
        return rendered

    @staticmethod
    def _result_for_ordinal(
        ordinal: int,
        prior_results: Mapping[str | int, Any],
    ) -> Any:
        for key in (f"step_{ordinal}", ordinal, ordinal - 1):
            if key in prior_results:
                return prior_results[key]
        raise KeyError(ordinal)

    def _resolve_reference(
        self,
        match: re.Match[str],
        *,
        step_index: int,
        prior_results: Mapping[str | int, Any],
    ) -> Any:
        ordinal = int(match.group("ordinal"))
        if ordinal > step_index:
            raise PlanFault(
                PlanFaultCode.MALFORMED_PLACEHOLDER,
                f"step {step_index + 1} may reference only a prior step, not step_{ordinal}",
                step_index=step_index,
            )
        try:
            current = self._result_for_ordinal(ordinal, prior_results)
        except KeyError as exc:
            raise PlanFault(
                PlanFaultCode.MISSING_REFERENCE,
                f"no recorded result exists for step_{ordinal}",
                step_index=step_index,
            ) from exc

        path = match.group("path")
        for component in path.split("."):
            if isinstance(current, Mapping):
                if component not in current:
                    current = None
                    break
                current = current[component]
            elif hasattr(current, component):
                current = getattr(current, component)
            else:
                current = None
                break
        if current is None:
            raise PlanFault(
                PlanFaultCode.MISSING_REFERENCE,
                f"step_{ordinal}.{path} is missing; the actor may not guess it",
                step_index=step_index,
            )
        if isinstance(current, Enum):
            return current.value
        return current


__all__ = [
    "CurrentPlan",
    "ExecutablePlanStep",
    "PlanFault",
    "PlanFaultCode",
    "PlanInvalidation",
    "PlanStep",
]
