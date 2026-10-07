"""Independent checks for unresolved review notes and native occurrence order."""
import json

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan
from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events
from test_benchmark_requirement_evaluator import banner, fixture, requirement
from test_httpcomponents_reviewed_metadata import reviewed_archive


def test_review_explicit_unresolved_obligation_cannot_become_complete(reviewed_archive):
    base, review, task, _ = reviewed_archive
    review["unresolved_obligations"] = ["A configured artifact attachment has not been reviewed."]
    path = base / ("review-" + review["project_id"] + ".json")
    path.write_text(json.dumps(review))
    draft = json.loads((base / "requirements" / (review["project_id"] + ".json")).read_text())
    with pytest.raises(ValueError, match="[Uu]nresolved"):
        apply_reviewed_plan(draft, task, path, base)


@pytest.mark.parametrize("reverse,expected", [(False, "passed"), (True, "unavailable")])
def test_required_native_bindings_must_preserve_relative_execution_order(fixture, reverse, expected):
    _, _, invocation, _, base, _ = fixture
    row = requirement("compile", "compile", "compiler:compile")
    row["validation"].update(
        goals=["compiler:compile", "compiler:testCompile"],
        native_bindings=[
            {"goal": "compiler:compile", "execution": "default", "occurrence": 0, "position": 0},
            {"goal": "compiler:testCompile", "execution": "default", "occurrence": 0, "position": 1},
        ],
    )
    goals = ["compiler:compile", "compiler:testCompile"]
    if reverse:
        goals.reverse()
    text = "".join(banner(goal) + "[INFO] Compiling 1 source file\n" for goal in goals) + "[INFO] BUILD SUCCESS\n"
    result = native_requirement(row, invocation, base, text, maven_events(text, terminal=True, serial=True))
    assert result["status"] == expected
