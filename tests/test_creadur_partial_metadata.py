"""Partial Creadur reviews keep non-JUnit tests and unusual artifact facts visible."""

import json
from collections import Counter
from copy import deepcopy

import pytest

from scripts.build_benchmark_requirements import ci_binding_inventory
from scripts.requirements_ci_sources import load_selected_ci_source
from scripts.requirements_metadata_inventory import append_known_requirements
from sag.benchmark.requirements import bound_file, summarize
from test_requirements_ci_timestamp_sources import archive


@pytest.mark.parametrize("name,modules,top,nested,pom", [
    ("creadur-whisker", 8, 144, 0, 160), ("creadur-rat", 7, 143, 7, 174),
])
def test_partial_review_preserves_every_source_bound_native_occurrence(archive, name, modules, top, nested, pom):
    review, base, kwargs = archive(name)
    result = load_selected_ci_source(review, base, **kwargs)
    actual, forks = ci_binding_inventory(result["text"])
    fields = ("goal", "version", "execution", "module", "occurrence", "position", "line")
    assert [{k: r[k] for k in fields} for r in review["ci_bindings"]] == actual
    assert [{k: r[k] for k in fields} for r in review["nested_ci_bindings"]] == forks
    assert (len(review["modules"]), len(actual), len(forks), len(review["pom_bindings"])) == (modules, top, nested, pom)
    assert review["annotation_completeness"]["status"] == "review_required"
    assert review["unresolved_obligations"]
    for source in [*review["sources"].values(), *(m["source_pom"] for m in review["modules"])]:
        assert bound_file(base, source).exists()


@pytest.mark.parametrize("name,formal,preparation", [
    ("creadur-whisker", 15, 13), ("creadur-rat", 14, 159),
])
def test_invoker_preparation_writes_are_not_final_artifact_denominators(archive, name, formal, preparation):
    review, _, _ = archive(name)
    inventory = review["install_write_inventory"]
    assert Counter(w["classification"] for w in inventory) == {
        "project_final_install": formal, "integration_fixture_preparation": preparation,
    }
    assert all(w["producer"]["goal"] == "install:install" for w in inventory if w["classification"] == "project_final_install")
    assert all(w["producer"]["goal"] == "invoker:install" for w in inventory if w["classification"] == "integration_fixture_preparation")
    installed = [a for r in review["requirements"] if r["kind"] == "install" for a in r["expectations"]["artifacts"]]
    assert len(installed) == formal
    assert len({a["repository_relative_path"] for a in installed}) == formal
    if name == "creadur-whisker":
        pom = next(a for a in installed if a["module"] == "apache-whisker-cli" and a["extension"] == "pom")
        assert pom["reviewed_source_relative_path"] == "apache-whisker-cli/dependency-reduced-pom.xml"
        main = next(r for r in review["requirements"] if r["id"] == "ci-step-1-apache-whisker-cli-main_archive")
        assert main["expectations"]["artifacts"][0]["path"] == "apache-whisker-cli/target/apache-whisker-0.2-SNAPSHOT.jar"
        assert [b["goal"] for b in review["ci_bindings"] if b.get("requirement_id") == main["id"]] == ["jar:jar", "shade:shade"]
    else:
        assert sum(a["classifier"] == "tests" for a in installed) == 1
        assert not any(a["classifier"] in {"sources", "jar-with-dependencies"} for a in installed)


@pytest.mark.parametrize("name,reported,invoker,unsupported", [
    ("creadur-whisker", 155, 10, 1), ("creadur-rat", 1149, 12, 2),
])
def test_non_junit_obligations_remain_in_appendable_incomplete_definition(archive, name, reported, invoker, unsupported):
    review, base, kwargs = archive(name)
    observations = review["ci_test_observations"]
    assert sum(x["reported"] for x in observations if x["kind"] == "surefire_cases") == reported
    assert sum(x["passed"] for x in observations if x["kind"] == "invoker_projects") == invoker
    if name == "creadur-rat":
        assert [s["reported"] for x in observations if x["kind"] == "antunit_targets" for s in x["suites"]] == [7, 21]
    draft = json.loads((base / "draft.json").read_text())
    previous = deepcopy(draft["requirements"])
    result = append_known_requirements(draft, kwargs["task"], base / "supplement/additional-known-requirements.json", base)
    assert result["requirements"][:len(previous)] == previous
    assert result["annotation_completeness"]["status"] == "review_required"
    unresolved_tests = [r for r in result["requirements"] if r["kind"] == "test" and r["validation"]["rule"] == "unclassified"]
    assert len(unresolved_tests) == unsupported
    for row in result["requirements"][len(previous):]:
        assert row["scope"]["status"] == "declared"
        assert row["validation"]["plan_resolved"] is False
        for source in row["sources"]:
            assert bound_file(base, source).exists()
    # Even if every currently listed outcome is green, this is only a known
    # lower bound: incomplete annotations cannot certify a whole task or stage.
    summary = summarize(result, [{"id": r["id"], "status": "passed"} for r in result["requirements"]], [], [])
    assert summary["status"] == "unavailable"
    assert set(summary["groups"].values()) == {"unavailable"}
