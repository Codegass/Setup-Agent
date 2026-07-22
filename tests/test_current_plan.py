import pytest
from pydantic import ValidationError

from sag.agent.current_plan import (
    CurrentPlan,
    PlanFault,
    PlanFaultCode,
    PlanInvalidation,
    PlanStep,
)


def _step(tool="project", params=None, *, preconditions=()):
    return {
        "tool": tool,
        "exact_params": params or {"action": "analyze"},
        "preconditions": list(preconditions),
        "expected_evidence": ["typed tool result"],
        "success_criteria": ["operation_outcome is success"],
    }


def test_current_plan_parses_typed_json_from_thinking_response():
    response = """THOUGHT: Clone and inspect without another reasoning turn.

CURRENT_PLAN:
```json
{
  "steps": [
    {
      "tool": "project",
      "exact_params": {"action": "clone", "repo_url": "https://example.test/repo.git"},
      "preconditions": [],
      "expected_evidence": ["workspace exists"],
      "success_criteria": ["clone completed"]
    },
    {
      "tool": "project",
      "exact_params": {"action": "analyze"},
      "preconditions": ["{{step_1.succeeded}}"],
      "expected_evidence": ["project brief ref"],
      "success_criteria": ["analysis completed"]
    }
  ],
  "invalidate_on": ["failure", "conflict", "unknown", "phase_change"]
}
```
"""

    plan = CurrentPlan.from_thinking_response(response)

    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "project"
    assert plan.invalidate_on == (
        PlanInvalidation.FAILURE,
        PlanInvalidation.CONFLICT,
        PlanInvalidation.UNKNOWN,
        PlanInvalidation.PHASE_CHANGE,
    )


def test_current_plan_rejects_missing_or_trailing_plan_payload():
    with pytest.raises(PlanFault, match="CURRENT_PLAN") as missing:
        CurrentPlan.from_thinking_response("THOUGHT: I have only prose.")
    assert missing.value.code is PlanFaultCode.MALFORMED_PLAN

    with pytest.raises(PlanFault, match="trailing content"):
        CurrentPlan.from_thinking_response(
            'CURRENT_PLAN: {"steps": [], "invalidate_on": []} actor should guess'
        )


def test_plan_step_is_strict_and_requires_executable_evidence_contract():
    with pytest.raises(ValidationError):
        PlanStep.model_validate(
            {
                "tool": "bash",
                "exact_params": {"command": "pwd"},
                "preconditions": [],
                "expected_evidence": [],
                "success_criteria": ["exit zero"],
                "params_sketch": "maybe pwd",
            }
        )

    with pytest.raises(ValidationError):
        PlanStep.model_validate(
            {
                "tool": "bash",
                "exact_params": {"command": "pwd"},
                "preconditions": [],
                "expected_evidence": [],
                "success_criteria": [],
            }
        )


def test_placeholder_resolution_feeds_prior_output_ref_without_actor_reanalysis():
    plan = CurrentPlan(
        steps=(
            PlanStep.model_validate(_step("file_io", {"action": "read", "file_path": "pom.xml"})),
            PlanStep.model_validate(
                _step(
                    "search",
                    {"target": "{{step_1.output_ref}}", "query": "maven.compiler.release"},
                    preconditions=("{{step_1.succeeded}}", "{{step_1.output_ref}}"),
                )
            ),
        )
    )

    resolved = plan.resolve_step(
        1,
        prior_results={"step_1": {"succeeded": True, "output_ref": "output_read_pom"}},
        available_tools={"file_io", "search"},
    )

    assert resolved.tool == "search"
    assert resolved.exact_params == {
        "target": "output_read_pom",
        "query": "maven.compiler.release",
    }
    assert "{{" not in resolved.model_dump_json()


def test_literal_nested_json_string_is_not_treated_as_a_placeholder():
    key_results = '{"build_system":"unknown","recommended":{"goal":"unknown"}}'
    plan = CurrentPlan(
        steps=(
            PlanStep.model_validate(
                _step(
                    "phase",
                    {
                        "action": "done",
                        "outcome": "unknown",
                        "key_results": key_results,
                    },
                )
            ),
        )
    )

    resolved = plan.resolve_step(
        0,
        prior_results={},
        available_tools={"phase"},
    )

    assert resolved.exact_params["key_results"] == key_results


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{{step_one.output_ref}}", "malformed placeholder"),
        ("{{step_2.output_ref}}", "prior step"),
        ("prefix {{step_1.output_ref", "malformed placeholder"),
    ],
)
def test_malformed_or_future_placeholder_is_a_scheduler_fault(value, message):
    plan = CurrentPlan(
        steps=(
            PlanStep.model_validate(_step("file_io")),
            PlanStep.model_validate(_step("search", {"target": value})),
        )
    )

    with pytest.raises(PlanFault, match=message) as caught:
        plan.resolve_step(
            1,
            prior_results={"step_1": {"output_ref": "output_one"}},
            available_tools={"file_io", "search"},
        )

    assert caught.value.code is PlanFaultCode.MALFORMED_PLACEHOLDER


def test_unknown_tool_or_unmet_precondition_faults_before_actor():
    unknown = CurrentPlan(steps=(PlanStep.model_validate(_step("invent_tool")),))
    with pytest.raises(PlanFault) as unknown_fault:
        unknown.resolve_step(0, prior_results={}, available_tools={"project"})
    assert unknown_fault.value.code is PlanFaultCode.UNKNOWN_TOOL

    unmet = CurrentPlan(
        steps=(
            PlanStep.model_validate(_step("project")),
            PlanStep.model_validate(
                _step("build", {"action": "compile"}, preconditions=("{{step_1.succeeded}}",))
            ),
        )
    )
    with pytest.raises(PlanFault) as unmet_fault:
        unmet.resolve_step(
            1,
            prior_results={"step_1": {"succeeded": False}},
            available_tools={"project", "build"},
        )
    assert unmet_fault.value.code is PlanFaultCode.UNMET_PRECONDITION


def test_reference_to_missing_prior_value_never_leaks_placeholder_to_actor():
    plan = CurrentPlan(
        steps=(
            PlanStep.model_validate(_step("project")),
            PlanStep.model_validate(_step("search", {"target": "{{step_1.output_ref}}"})),
        )
    )

    with pytest.raises(PlanFault) as caught:
        plan.resolve_step(
            1,
            prior_results={"step_1": {"succeeded": True, "output_ref": None}},
            available_tools={"project", "search"},
        )

    assert caught.value.code is PlanFaultCode.MISSING_REFERENCE
