"""The reactor review uses complete, portable archive fixtures, not local runs."""
import json
from pathlib import Path
import re
import zipfile

import pytest

from scripts.build_benchmark_requirements import ci_binding_inventory, pom_execution_bindings, task_digest, text_at, xml_root
from sag.benchmark.requirements import bound_file, repository_artifact_path


@pytest.fixture(params=["httpcomponents-client", "httpcomponents-core"])
def reviewed_archive(tmp_path, request):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_httpcomponents_metadata.zip") as archive:
        archive.extractall(tmp_path)
    project = request.param
    review = json.loads((tmp_path / f"review-{project}.json").read_text())
    task = json.loads((tmp_path / "tasks" / f"{project}.json").read_text())
    files = {name: bound_file(tmp_path, source) for name, source in review["sources"].items()}
    return tmp_path, review, task, files


def test_all_reactor_modules_and_pom_executions_have_pinned_source_provenance(reviewed_archive):
    base, review, task, files = reviewed_archive
    assert review["task_sha256"] == task_digest(task)
    assert review["commit"] == task["sha"]
    root = xml_root(files["effective_pom"].read_bytes())
    actual = []
    assert len(root) == len(review["modules"])
    for model, module in zip(root, review["modules"]):
        assert module["id"] == text_at(model, "artifactId")
        source = xml_root(bound_file(base, module["source_pom"]).read_bytes())
        assert text_at(source, "artifactId") == module["id"]
        relative = "" if module["path"] == "." else "/" + module["path"]
        assert text_at(model, "build/directory") == review["metadata_workspace"] + relative + "/target"
        actual.extend({**p, "module": module["id"]} for p in pom_execution_bindings(model))
    fields = ("module", "plugin", "version", "goal", "execution", "phase")
    assert [{k: x[k] for k in fields} for x in review["pom_bindings"]] == actual
    assert all(p["disposition"] in {"observed", "outside_task_lifecycle"} and p["reason"] for p in review["pom_bindings"])


def test_native_occurrences_are_complete_and_never_collapsed(reviewed_archive):
    _, review, _, files = reviewed_archive
    actual, nested = ci_binding_inventory(files["official_ci"].read_text())
    fields = ("goal", "version", "execution", "module", "occurrence", "position", "line")
    assert [{k: b[k] for k in fields} for b in review["ci_bindings"]] == actual
    assert not nested
    keys = {r["id"] for r in review["requirements"]}
    assigned = {b.get("requirement_id") for b in review["ci_bindings"] if b["disposition"] == "requirement"}
    assert assigned == keys
    for row in review["requirements"]:
        matches = [b for b in review["ci_bindings"] if b.get("requirement_id") == row["id"] and b["disposition"] == "requirement"]
        assert {b["module"] for b in matches} == {row["module"]}
        expected = [0] if row["kind"] in {"test", "install"} else [0, 1]
        assert [b["occurrence"] for b in matches] == expected


def test_each_test_pool_executes_once_and_retains_explicit_reuse_witness(reviewed_archive):
    _, review, _, files = reviewed_archive
    bindings = review["ci_bindings"]
    lines = files["official_ci"].read_text().splitlines()
    tests = [r for r in review["requirements"] if r["validation"]["rule"] == "junit"]
    reuse = [b for b in bindings if b["disposition"] == "same_configuration_reuse"]
    assert len(tests) == len(reuse) == len(review["modules"]) - 1
    assert len({tuple(r["validation"]["report_directories"]) for r in tests}) == len(tests)
    for b in reuse:
        first = bindings[b["reuse_of"]["position"]]
        assert first["disposition"] == "requirement"
        assert first["requirement_id"] == b["requirement_id"]
        assert all(first[k] == b[k] for k in ("module", "goal", "execution", "version"))
        assert first["occurrence"] == 0 and b["occurrence"] == 1
        end = bindings[b["position"] + 1]["line"] - 1
        segment = "\n".join(lines[b["line"]:end])
        assert b["reuse_witness"] in segment


def test_disabled_quality_checks_require_byte_bound_pom_or_native_witness(reviewed_archive):
    _, review, _, files = reviewed_archive
    models = {text_at(m, "artifactId"): m for m in xml_root(files["effective_pom"].read_bytes())}
    bindings = review["ci_bindings"]
    lines = files["official_ci"].read_text().splitlines()
    for b in bindings:
        if b["disposition"] != "ci_disabled":
            continue
        assert "requirement_id" not in b
        witness = b["disabled_witness"]
        if witness["type"] == "effective_pom_configuration":
            assert witness["effective_pom_sha256"] == review["sources"]["effective_pom"]["sha256"]
            assert all(witness[k] == b[k] for k in ("module", "goal", "execution"))
            plugin = next(p for p in models[b["module"]].findall("build/plugins/plugin") if text_at(p, "artifactId") == witness["plugin"])
            assert text_at(plugin, witness["path"]) == witness["value"] == "true"
        else:
            end = bindings[b["position"] + 1]["line"] - 1
            assert witness["text"] in "\n".join(lines[b["line"]:end])


def test_exact_install_write_inventory_includes_pom_main_and_attachments(reviewed_archive):
    _, review, _, files = reviewed_archive
    writes = re.findall(r"^\[INFO\] Installing (.+) to (.+)\s*$", files["official_ci"].read_text(), re.M)
    artifacts = [a for r in review["requirements"] if r["kind"] == "install" for a in r["expectations"]["artifacts"]]
    assert len(artifacts) == len(writes)
    expected = {repository_artifact_path(a): a for a in artifacts}
    assert len(expected) == len(artifacts)
    for source, destination in writes:
        assert len([p for p in expected if destination.endswith("/" + p)]) == 1
    packaged = [a for r in review["requirements"] if r["kind"] == "package" for a in r["expectations"]["artifacts"]]
    assert len(packaged) == len(artifacts) - len(review["modules"])
    parent = review["modules"][0]["id"]
    assert not [a for a in packaged if a["module"] == parent and a["role"] == "main"]
    assert [a["classifier"] for a in packaged if a["module"] == parent] == ["site"]


def test_logical_dependency_graph_does_not_invent_cross_pass_linear_order(reviewed_archive):
    _, review, _, _ = reviewed_archive
    rows = {r["id"]: r for r in review["requirements"]}
    visited = set()
    def visit(key, active):
        assert key not in active
        if key in visited:
            return
        for dep in rows[key]["depends_on"]:
            visit(dep, active | {key})
        visited.add(key)
    for key in rows:
        visit(key, set())
    for row in rows.values():
        if row["kind"] != "install":
            assert all(rows[d]["module"] != row["module"] and rows[d]["kind"] == "install" for d in row["depends_on"])
