"""Review-required benchmark definitions frozen from the acquired dataset."""

import json

import pytest


# ------------------------------------------------------ harness and CLI

def test_harness_admits_review_required_only_with_the_flag():
    from scripts.run_portable_harness import REVIEW_ENV, admit_definition, build_parser, review_environment

    pending = {"annotation_completeness": {"status": "review_required", "gaps": ["x"]}}
    with pytest.raises(ValueError, match="review must be complete"):
        admit_definition(pending, allow_review_required=False)
    admission = admit_definition(pending, allow_review_required=True)
    assert admission == {"status": "review_required", "admission": "allowed_by_review_required_flag"}
    assert review_environment(admission) == {REVIEW_ENV: "1"} == {"SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED": "1"}
    complete = admit_definition({"annotation_completeness": {"status": "complete", "gaps": []}},
                                allow_review_required=True)
    assert complete == {"status": "complete", "admission": "complete"}
    assert review_environment(complete) == {}
    required = ["--project", "p", "--output", "o", "--run-id", "r", "--arm", "sag", "--image", "i"]
    assert build_parser().parse_args(required).allow_review_required is False
    assert build_parser().parse_args(required + ["--allow-review-required"]).allow_review_required is True


@pytest.mark.parametrize("value,accepted", [("1", True), ("0", False), (None, False)])
def test_sag_cli_admits_review_required_only_when_the_harness_sets_the_switch(tmp_path, monkeypatch, value, accepted):
    from test_requirements_protocol_surfaces import RecordingSetupAgent, forbid_container_work, inputs, invoke

    _task, spec, _target, paths, flags = inputs(tmp_path)
    spec["annotation_completeness"] = {"status": "review_required", "gaps": ["unreviewed"]}
    paths["requirements"].write_text(json.dumps(spec))
    if value is None:
        monkeypatch.delenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("SAG_BENCHMARK_ALLOW_REVIEW_REQUIRED", value)
    if not accepted:
        forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    if accepted:
        assert result.exit_code == 0, result.output
        assert len(RecordingSetupAgent.calls) == 1
    else:
        assert result.exit_code != 0
        assert "annotation still needs review" in result.output
        assert RecordingSetupAgent.calls == []
