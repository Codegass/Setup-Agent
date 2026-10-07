"""Source-bound review of the frozen Tomcat test endpoint, without Maven/network."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan, ci_native_text
from sag.benchmark.native_evidence import maven_events


@pytest.fixture
def tomcat_review(tmp_path):
    fixture = Path(__file__).parent / "fixtures/requirements_reviewed_tomcat_metadata.zip"
    with ZipFile(fixture) as archive:
        draft = json.loads(archive.read("draft.json"))
        task = json.loads(archive.read("task.json"))
        review = json.loads(archive.read("review.json"))
        for ref in review["sources"].values():
            path = tmp_path / ref["path"]
            assert path.resolve().is_relative_to(tmp_path.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(ref["path"]))
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review))
    return draft, task, review, path, tmp_path


def test_test_fixture_jars_do_not_turn_test_endpoint_into_distribution_package(tomcat_review):
    draft, task, review, path, destination = tomcat_review
    result = apply_reviewed_plan(draft, task, path, destination)
    assert result["annotation_completeness"]["status"] == "complete"
    assert len(result["requirements"]) == 7
    assert set(result["group_labels"]) == {"compile", "test", "quality_check"}
    assert result["ci_alignment"]["comparison_admitted"] is False
    assert result["ci_alignment"]["reference_status"] == "official_record_with_evidence_gaps"
    fixture_binding = next(b for b in review["ci_bindings"] if b["execution"] == "create-test-jars")
    assert fixture_binding["disposition"] == "support"
    fixture_exception = next(e for e in review["artifact_scope_exceptions"] if e["classification"] == "test_fixture_support")
    assert len(fixture_exception["paths"]) == 5
    assert all(p.startswith("target/test-classes/") for p in fixture_exception["paths"])
    assert not any(r["expectations"].get("artifacts") for r in result["requirements"])
    assert result["preconditions"][1]["steps"][0]["java_major"] == 17


def test_every_tomcat_requirement_maps_to_one_official_top_level_native_outcome(tomcat_review):
    draft, task, review, path, destination = tomcat_review
    result = apply_reviewed_plan(draft, task, path, destination)
    assert len(review["pom_bindings"]) == 22
    assert len(review["ci_bindings"]) == 12
    assert review["nested_ci_bindings"] == []
    raw = (destination / review["sources"]["official_ci"]["path"]).read_text()
    events = maven_events(ci_native_text(raw), terminal=True, serial=True)
    for requirement in result["requirements"]:
        validation = requirement["validation"]
        matching = [e for e in events if e["module"] == requirement["module"]
                    and e["goal"] in validation["goals"]
                    and e["execution"] == validation["execution"]
                    and e["occurrence"] == validation["occurrence"]]
        assert len(matching) == 1, requirement["id"]
        assert matching[0]["status"] == "passed", requirement["id"]


def test_omitting_fixture_creation_cannot_claim_complete_execution_review(tomcat_review):
    draft, task, review, path, destination = tomcat_review
    review["ci_bindings"] = [b for b in review["ci_bindings"] if b["execution"] != "create-test-jars"]
    path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="Every ordered CI occurrence"):
        apply_reviewed_plan(draft, task, path, destination)
