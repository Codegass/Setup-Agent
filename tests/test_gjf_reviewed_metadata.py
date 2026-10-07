"""Import the actual archived multi-command CI scope, without building GJF."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan, build_project
from sag.benchmark.requirements import bound_file, load_json


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


@pytest.fixture
def archived_gjf(tmp_path):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_gjf_metadata.zip") as z:
        assert all((tmp_path / p).resolve().is_relative_to(tmp_path) for p in z.namelist())
        z.extractall(tmp_path)
    manifest = load_json(tmp_path / "import-inputs/manifest.json")
    task = load_json(tmp_path / "task.json")
    draft = build_project(manifest["projects"][0], tmp_path / "import-inputs", tmp_path, Path(__file__).resolve().parents[1])
    return tmp_path, task, draft


def test_real_gjf_scope_keeps_both_commands_modules_and_physical_requirements(archived_gjf):
    base, task, draft = archived_gjf
    spec = apply_reviewed_plan(draft, task, base / "review.json", base)
    assert spec["annotation_completeness"]["status"] == "complete"
    assert len(spec["requirements"]) == 21
    assert [len(s["ci_bindings"]) for s in spec["reviewed_execution_plan"]["steps"]] == [33, 19]
    assert len(spec["compilation_reuse"]) == 1
    tests = [r for r in spec["requirements"] if r["kind"] == "test"]
    assert len(tests) == 1 and tests[0]["step_id"] == "ci-step-2"
    installs = [r for r in spec["requirements"] if r["kind"] == "install"]
    assert sum(len(r["expectations"]["artifacts"]) for r in installs) == 9
    generated = next(r for r in installs if r["module"] == "google-java-format-eclipse-plugin")
    assert generated["validation"]["goals"] == ["tycho-packaging:update-consumer-pom", "install:install", "tycho-p2-plugin:update-local-index"]
    assert not any(a.get("path", "").endswith(".tycho-consumer-pom.xml")
                   for r in spec["requirements"] for a in r.get("expectations", {}).get("artifacts", []))
    assert task["steps"][0]["argv"] == ["mvn", "install", "-DskipTests=true", "-Dmaven.javadoc.skip=true", "-B", "-V"]
    assert task["steps"][1]["argv"] == ["mvn", "test", "-B"]


@pytest.mark.parametrize("mutation", ["descriptor", "generated_pom", "jvm_source", "ci_job", "ci_identity"])
def test_gjf_review_rejects_missing_or_conflicting_source_authority(archived_gjf, mutation):
    base, task, draft = archived_gjf
    outer = load_json(base / "review.json")
    entry = outer["step_reviews"][0]
    path = bound_file(base, entry["review"])
    review = load_json(path)
    if mutation == "descriptor": review.pop("plugin_descriptors")
    elif mutation == "generated_pom": review.pop("generated_install_poms")
    elif mutation == "jvm_source": review["additional_sources"]["launcher_source:.mvn/jvm.config"]["sha256"] = "0" * 64
    else:
        reference = draft["ci_alignment"]["archived_ci_index"]
        index_path = bound_file(base, reference)
        index = load_json(index_path)
        if mutation == "ci_job": index["sources"]["job_log"]["sha256"] = "0" * 64
        else: index["repo"] = "other/project"
        updated = save(index_path, index)
        reference.update(sha256=updated["sha256"], bytes=updated["bytes"])
    entry["review"] = save(path, review)
    save(base / "review.json", outer)
    with pytest.raises(ValueError): apply_reviewed_plan(draft, task, base / "review.json", base)
