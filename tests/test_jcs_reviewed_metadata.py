"""JCS's declared main artifacts and real tests survive its explicit no-ops."""
import json
from pathlib import Path
import re
import zipfile

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan, ci_binding_inventory, pom_execution_bindings, task_digest, text_at, xml_root
from sag.benchmark.requirements import bound_file, repository_artifact_path


@pytest.fixture
def reviewed_jcs(tmp_path):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_jcs_metadata.zip") as archive:
        archive.extractall(tmp_path)
    review = json.loads((tmp_path / "review-commons-jcs.json").read_text())
    files = {name: bound_file(tmp_path, source) for name, source in review["sources"].items()}
    return tmp_path, review, files


def test_jcs_all_source_and_native_bindings_are_preserved(reviewed_jcs):
    base, review, files = reviewed_jcs
    task = json.loads((base / "tasks/commons-jcs.json").read_text())
    assert review["task_sha256"] == task_digest(task)
    actual, nested = ci_binding_inventory(files["official_ci"].read_text())
    fields = ("goal", "version", "execution", "module", "occurrence", "position", "line")
    assert [{k: b[k] for k in fields} for b in review["ci_bindings"]] == actual
    assert not nested
    declarations = []
    models = list(xml_root(files["effective_pom"].read_bytes()))
    assert len(models) == len(review["modules"]) == 7
    for model, module in zip(models, review["modules"]):
        source = xml_root(bound_file(base, module["source_pom"]).read_bytes())
        assert text_at(source, "artifactId") == text_at(model, "artifactId") == module["id"]
        declarations.extend({**p, "module": module["id"]} for p in pom_execution_bindings(model))
    fields = ("goal", "version", "execution", "module", "plugin", "phase")
    assert [{k: p[k] for k in fields} for p in review["pom_bindings"]] == declarations


def test_jcs_tck_no_production_sources_does_not_erase_main_jar_or_real_test(reviewed_jcs):
    _, review, _ = reviewed_jcs
    module = "commons-jcs4-jcache-tck"
    events = [b for b in review["ci_bindings"] if b["module"] == module]
    compile_event = next(b for b in events if b["goal"] == "compiler:compile")
    assert compile_event["disposition"] == "ci_disabled"
    assert compile_event["disabled_witness"]["text"] == "No sources to compile"
    main = next(r for r in review["requirements"] if r["module"] == module and r["subtype"] == "main_archive")
    assert {b["goal"] for b in events if b.get("requirement_id") == main["id"]} == {"bundle:manifest", "jar:jar", "moditect:add-module-info"}
    assert main["expectations"]["artifacts"][0]["path"] == module + "/target/" + module + "-4.0.0-SNAPSHOT.jar"
    assert len([r for r in review["requirements"] if r["module"] == module and r["kind"] == "test"]) == 1
    assert next(o for o in review["ci_test_observations"] if o["module"] == module)["total"] == 1
    assert not [r for r in review["requirements"] if r["module"] == module and r["subtype"] in {"test_archive", "source_archive", "test_source_archive"}]


def test_jcs_disabled_bindings_have_exact_native_witness_and_no_outcome(reviewed_jcs):
    _, review, files = reviewed_jcs
    lines = files["official_ci"].read_text().splitlines()
    events = review["ci_bindings"]
    for b in events:
        if b["disposition"] == "ci_disabled":
            assert "requirement_id" not in b
            end = events[b["position"] + 1]["line"] - 1 if b["position"] + 1 < len(events) else len(lines)
            assert b["disabled_witness"]["text"] in "\n".join(lines[b["line"]:end])
    assert not [r for r in review["requirements"] if "coverage" in r["subtype"]]
    changes = [r for r in review["requirements"] if r["subtype"] == "changes_document_validation"]
    assert [r["module"] for r in changes] == ["commons-jcs4"]


def test_jcs_install_inventory_preserves_classifier_and_sbom_exceptions(reviewed_jcs):
    _, review, files = reviewed_jcs
    writes = re.findall(r"^\[INFO\] Installing (.+) to (.+)\s*$", files["official_ci"].read_text(), re.M)
    installed = [a for r in review["requirements"] if r["kind"] == "install" for a in r["expectations"]["artifacts"]]
    assert len(installed) == len(writes) == 46
    paths = {repository_artifact_path(a) for a in installed}
    assert len(paths) == 46
    assert all(len([p for p in paths if destination.endswith("/" + p)]) == 1 for _, destination in writes)
    dist = [a for a in installed if a["module"] == "commons-jcs4-dist"]
    assert {a["extension"] for a in dist} == {"pom", "spdx.json"}
    assert not any(a["classifier"] == "cyclonedx" for a in dist)
    jcache = [a for a in installed if a["module"] == "commons-jcs4-jcache"]
    assert {"cdi", "nocdi", "tests", "sources", "test-sources", "cyclonedx"} <= {a["classifier"] for a in jcache}


def test_jcs_main_archives_require_final_module_processing_not_a_second_artifact(reviewed_jcs):
    _, review, _ = reviewed_jcs
    main = [r for r in review["requirements"] if r["subtype"] == "main_archive"]
    assert len(main) == 5
    for row in main:
        assert len(row["expectations"]["artifacts"]) == 1
        events = [b for b in review["ci_bindings"] if b.get("requirement_id") == row["id"]]
        assert [b["goal"] for b in events] == ["bundle:manifest", "jar:jar", "moditect:add-module-info"]
    assert sum(o["total"] for o in review["ci_test_observations"]) == 443


def import_jcs(archive):
    base, review, _ = archive
    path = base / "review-commons-jcs.json"
    path.write_text(json.dumps(review))
    task = json.loads((base / "tasks/commons-jcs.json").read_text())
    draft = json.loads((base / "requirements/commons-jcs.json").read_text())
    return apply_reviewed_plan(draft, task, path, base)


def test_jcs_import_preserves_resource_jar_real_tests_and_all_install_coordinates(reviewed_jcs):
    spec = import_jcs(reviewed_jcs)
    assert spec["annotation_completeness"]["status"] == "complete"
    assert len(spec["requirements"]) == 84
    tck = [r for r in spec["requirements"] if r["module"] == "commons-jcs4-jcache-tck"]
    assert not any(r["subtype"] == "production_sources" for r in tck)
    assert {"test_sources", "unit", "main_archive"} <= {r["subtype"] for r in tck}
    main = next(r for r in tck if r["subtype"] == "main_archive")
    assert main["validation"]["goals"] == ["bundle:manifest", "jar:jar", "moditect:add-module-info"]
    assert len(main["validation"]["native_bindings"]) == 3
    installed = [a for r in spec["requirements"] if r["kind"] == "install" for a in r["expectations"]["artifacts"]]
    assert len({repository_artifact_path(a) for a in installed}) == 46
    assert not any("jacoco:check" in r["validation"]["goals"] for r in spec["requirements"])
    assert sum(len(r["validation"]["native_bindings"]) for r in spec["requirements"]) == 94


@pytest.mark.parametrize("mutation", ["native_occurrence", "pom_declaration", "installed_spdx", "moditect_binding", "false_no_source"])
def test_jcs_omission_or_false_nonapplicability_cannot_make_definition_complete(reviewed_jcs, mutation):
    _, review, _ = reviewed_jcs
    if mutation == "native_occurrence":
        review["ci_bindings"].pop()
    elif mutation == "pom_declaration":
        review["pom_bindings"].pop()
    elif mutation == "installed_spdx":
        row = next(r for r in review["requirements"] if r["kind"] == "install")
        row["expectations"]["artifacts"][:] = [a for a in row["expectations"]["artifacts"] if a["extension"] != "spdx.json"]
    elif mutation == "moditect_binding":
        review["ci_bindings"].pop(next(i for i,b in enumerate(review["ci_bindings"]) if b["goal"] == "moditect:add-module-info" and b["disposition"] == "requirement"))
    else:
        binding = next(b for b in review["ci_bindings"] if b["goal"] == "compiler:testCompile")
        binding.update(disposition="ci_disabled", disabled_witness={"type": "native_log", "text": "No sources to compile"})
        binding.pop("requirement_id")
    with pytest.raises(ValueError):
        import_jcs(reviewed_jcs)
