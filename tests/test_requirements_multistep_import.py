"""Composition keeps every command's independently bound configuration and scope."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import pytest

from scripts.build_benchmark_requirements import (
    _reviewed_gha_command, _reviewed_idle_pom, apply_reviewed_plan, task_digest,
)
from sag.benchmark.requirements import normalize_task, validate_requirements


def save(base, relative, value):
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(data)
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@pytest.fixture
def composed(tmp_path):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_metadata.zip") as archive:
        prefix = "commons-dbutils/"
        original_draft, original_task, original_review = [json.loads(archive.read(prefix + name + ".json"))
                                                        for name in ("draft", "task", "review")]
        task = normalize_task(original_task)
        first = {**task["steps"][0], "id": "first"}
        second = {**first, "id": "second", "argv": [*first["argv"], "-Duser.language=en"]}
        task["steps"] = [first, second]
        draft = deepcopy(original_draft)
        draft["task_sha256"] = task_digest(task)
        draft["requirements"] = []
        draft["steps"] = []
        draft["preconditions"][1]["steps"] = []
        children = []
        for step in task["steps"]:
            review = deepcopy(original_review)
            review.update(task_sha256=task_digest(task), step_id=step["id"])
            for source_name, source in list(review["sources"].items()):
                raw = archive.read(prefix + source["path"])
                if source_name == "preparation_request":
                    request = json.loads(raw)
                    request["original_task"] = step
                    raw = json.dumps(request).encode()
                review["sources"][source_name] = save(tmp_path, step["id"] + "/" + source["path"], raw)
            mapping = {row["id"]: step["id"] + "-" + row["id"] for row in review["requirements"]}
            for row in review["requirements"]:
                row["id"] = mapping[row["id"]]
                row["depends_on"] = [mapping[x] for x in row["depends_on"]]
            for binding in review["ci_bindings"]:
                if binding.get("requirement_id"):
                    binding["requirement_id"] = mapping[binding["requirement_id"]]
            for old in original_draft["requirements"]:
                row = deepcopy(old)
                row["step_id"] = step["id"]
                row["id"] = step["id"] + "-" + row["id"]
                row["depends_on"] = [step["id"] + "-" + x for x in row.get("depends_on", [])]
                draft["requirements"].append(row)
            metadata = deepcopy(original_draft["steps"][0])
            metadata.update(step_id=step["id"], argv=step["argv"])
            draft["steps"].append(metadata)
            draft["preconditions"][1]["steps"].append({"step_id": step["id"], "java_major": step["java_major"], "maven_version": step["maven_version"]})
            source = save(tmp_path, step["id"] + "-review.json", review)
            children.append({"step_id": step["id"], "review": source})
    outer = {"schema_version": 1, "review_protocol": "multi-step-maven-v1",
             "project_id": draft["project_id"], "commit": task["sha"], "task_sha256": task_digest(task),
             "reviewed_by": "synthetic regression fixture", "review_notes": "Both original commands remain mandatory",
             "unresolved_obligations": [], "step_reviews": children}
    path = tmp_path / "review.json"
    path.write_text(json.dumps(outer))
    return tmp_path, draft, task, outer, path


def test_multi_step_import_combines_all_requirements_and_preserves_parent_identity(composed):
    base, draft, task, _, path = composed
    result = apply_reviewed_plan(draft, task, path, base / "out")
    validate_requirements(result, task)
    assert result["annotation_completeness"]["status"] == "complete"
    assert result["task_sha256"] == task_digest(task)
    assert {r["step_id"] for r in result["requirements"]} == {"first", "second"}
    assert len(result["requirements"]) == 2 * sum(r["step_id"] == "first" for r in result["requirements"])
    assert [x["step_id"] for x in result["reviewed_execution_plan"]["steps"]] == ["first", "second"]
    assert result["steps"][1]["argv"][-1] == "-Duser.language=en"
    assert result["preconditions"] == draft["preconditions"]


@pytest.mark.parametrize("mutation", ["drop", "swap", "duplicate", "unresolved", "tamper_child", "wrong_child_step", "wrong_parent_digest", "changed_step_config", "pool_id_collision"])
def test_composition_rejects_missing_reordered_changed_or_reused_step_evidence(composed, mutation):
    base, draft, task, outer, path = composed
    if mutation == "drop": outer["step_reviews"].pop()
    elif mutation == "swap": outer["step_reviews"].reverse()
    elif mutation == "duplicate": outer["step_reviews"][1] = outer["step_reviews"][0]
    elif mutation == "unresolved": outer["unresolved_obligations"] = ["missing module"]
    elif mutation == "wrong_parent_digest": outer["task_sha256"] = "0" * 64
    else:
        item = outer["step_reviews"][1]
        child_path = base / item["review"]["path"]
        child = json.loads(child_path.read_text())
        if mutation == "tamper_child": child["review_notes"] = "changed bytes"
        elif mutation == "wrong_child_step": child["step_id"] = "first"
        elif mutation == "changed_step_config":
            req_ref = child["sources"]["preparation_request"]
            request = json.loads((base / req_ref["path"]).read_text())
            request["original_task"]["argv"].append("-DskipTests")
            child["sources"]["preparation_request"] = save(base, req_ref["path"], request)
        else:
            for row in child["requirements"]:
                row["id"] = row["id"].replace("second-", "first-", 1)
                row["depends_on"] = [x.replace("second-", "first-", 1) for x in row["depends_on"]]
            for binding in child["ci_bindings"]:
                if binding.get("requirement_id"):
                    binding["requirement_id"] = binding["requirement_id"].replace("second-", "first-", 1)
        updated = save(base, item["review"]["path"], child)
        if mutation != "tamper_child": item["review"] = updated
    path.write_text(json.dumps(outer))
    with pytest.raises(ValueError): apply_reviewed_plan(draft, task, path, base / "out")


def gha_fixture(tmp_path):
    first = "2026-09-22T01:00:00Z ##[group]Run mvn install -DskipTests=true\n2026-09-22T01:00:01Z [INFO] BUILD SUCCESS\n"
    second = "2026-09-22T01:00:02Z ##[group]Run mvn test\n2026-09-22T01:00:03Z [INFO] BUILD SUCCESS\n"
    job = save(tmp_path, "job.log", (first + second).encode())
    segment = save(tmp_path, "selected.log", second.encode())
    review = {"ci_source": {"kind": "github-actions-command-v1", "raw_job": job, "start_line": 3, "end_line_exclusive": 5}, "sources": {"official_ci": segment}}
    return review, {"ci_alignment": {"selected_url": "https://github.com/o/r/actions/runs/1/job/2"}}, {"steps": [{"argv": ["mvn", "test"]}]}, {job["sha256"]}


def test_gha_slice_is_exact_command_but_not_whole_job(composed):
    base, *_ = composed
    review, spec, task, hashes = gha_fixture(base)
    value = _reviewed_gha_command(review, base, spec, task, hashes)
    assert value["lineage"]["start_line"] == 3
    assert value["variant"] is None


@pytest.mark.parametrize("mutation", ["unanchored", "truncate", "change_command", "change_bytes", "overlap"])
def test_gha_cannot_select_favorable_subset_or_wrong_command(tmp_path, mutation):
    review, spec, task, hashes = gha_fixture(tmp_path)
    if mutation == "unanchored": hashes = set()
    elif mutation == "truncate": review["ci_source"]["end_line_exclusive"] = 4
    elif mutation == "change_command": task["steps"][0]["argv"] = ["mvn", "test", "-DskipTests"]
    elif mutation == "change_bytes": review["sources"]["official_ci"] = save(tmp_path, "selected.log", b"[INFO] BUILD SUCCESS\n")
    else: review["ci_source"]["start_line"] = 1
    with pytest.raises(ValueError): _reviewed_gha_command(review, tmp_path, spec, task, hashes)


def test_parent_pom_can_have_no_goals_without_omission():
    model = ET.fromstring("<project><artifactId>parent</artifactId><name>Parent</name><version>1</version><packaging>pom</packaging></project>")
    witness = "[INFO] Parent 1 ........ SUCCESS [ 0.001 s]"
    module = {"id": "parent", "no_ci_bindings": {"disposition": "pom_without_goal_execution", "reason": "test has no parent bindings", "reactor_summary_witness": witness}}
    log = "[INFO] Reactor Summary:\n" + witness + "\n[INFO] BUILD SUCCESS\n"
    _reviewed_idle_pom(module, model, log, ["test"])
    for goals in (["install"], ["javadoc:jar"], []):
        with pytest.raises(ValueError): _reviewed_idle_pom(module, model, log, goals)
    model.find("packaging").text = "eclipse-plugin"
    with pytest.raises(ValueError): _reviewed_idle_pom(module, model, log, ["test"])
    model.find("packaging").text = "pom"
    with pytest.raises(ValueError): _reviewed_idle_pom(module, model, log.replace("SUCCESS", "SKIPPED"), ["test"])


@pytest.mark.parametrize(('goal','text'), [
    ('compiler:testCompile', '[INFO] No sources to compile'),
    ('surefire:test', '[INFO] No tests to run.'),
    ('surefire:test', '[INFO] Tests are skipped.'),
    ('artifact:buildinfo', '[INFO] Auto-skipping goal because module skips install and/or deploy'),
    ('javadoc:jar', '[INFO] Not executing Javadoc as the project is not a Java classpath-capable package'),
])
def test_inapplicable_native_witness_is_bound_to_exact_goal_and_source(goal, text):
    from scripts.build_benchmark_requirements import reviewed_disabled_binding
    binding = {'goal': goal, 'disabled_witness': {'type': 'native_log', 'text': text}}
    reviewed_disabled_binding(binding, text + '\n', ET.fromstring('<project/>'), 'a' * 64)
    with pytest.raises(ValueError):
        reviewed_disabled_binding(binding, '[INFO] unrelated\n', ET.fromstring('<project/>'), 'a' * 64)
    binding['goal'] = 'enforcer:enforce'
    with pytest.raises(ValueError):
        reviewed_disabled_binding(binding, text + '\n', ET.fromstring('<project/>'), 'a' * 64)


def test_multistep_capture_request_can_bind_each_original_step(composed):
    base,draft,task,outer,path=composed
    for child in outer['step_reviews']:
        review=json.loads((base/child['review']['path']).read_text())
        ref=review['sources']['preparation_request']
        request=json.loads((base/ref['path']).read_text())
        request.update(original_task=task['steps'][0],original_steps=task['steps'])
        review['sources']['preparation_request']=save(base,ref['path'],request)
        child['review']=save(base,child['review']['path'],review)
    path.write_text(json.dumps(outer))
    result=apply_reviewed_plan(draft,task,path,base/'out')
    assert result['annotation_completeness']['status']=='complete'
    child=outer['step_reviews'][1]
    review=json.loads((base/child['review']['path']).read_text())
    ref=review['sources']['preparation_request'];request=json.loads((base/ref['path']).read_text())
    request['original_steps'][1]['argv'].append('-DskipTests')
    review['sources']['preparation_request']=save(base,ref['path'],request)
    child['review']=save(base,child['review']['path'],review);path.write_text(json.dumps(outer))
    with pytest.raises(ValueError,match='changed the original multi-step'):
        apply_reviewed_plan(draft,task,path,base/'out')
