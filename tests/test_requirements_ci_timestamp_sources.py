"""Source-bound Creadur command extraction; no CI requests or lifecycle runs."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan, ci_binding_inventory
from scripts.requirements_ci_sources import (
    extract_timestamped_jenkins_command, load_selected_ci_source,
    project_declared_variant_bindings,
)
from sag.benchmark.requirements import bound_file


def ref(path, base):
    raw = path.read_bytes()
    return {"path": str(path.relative_to(base)), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


@pytest.fixture
def archive(tmp_path):
    def load(name="creadur-tentacles"):
        base = tmp_path / name
        base.mkdir()
        with ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_creadur_metadata.zip") as z:
            for item in z.namelist():
                if item.startswith(name + "/"):
                    path = base / item.removeprefix(name + "/")
                    assert path.resolve().is_relative_to(base.resolve())
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(z.read(item))
        review = json.loads((base / "review.json").read_text())
        task = json.loads((base / "task.json").read_text())
        index = json.loads((base / "frozen-index.json").read_text())
        kwargs = {"frozen_ci_index_ref": ref(base / "frozen-index.json", base),
                  "frozen_ci_index_base": base, "task": task, "selected_url": index["selected_url"]}
        return review, base, kwargs
    return load


@pytest.mark.parametrize("name,first,last,top_count", [
    ("creadur-tentacles", 149, 264, 17),
    ("creadur-whisker", 151, 955, 144),
    ("creadur-rat", 757, 6465, 143),
])
def test_existing_archives_preserve_exact_single_command_byte_and_line_boundaries(archive, name, first, last, top_count):
    review, base, kwargs = archive(name)
    result = load_selected_ci_source(review, base, **kwargs)
    lineage = result["lineage"]
    assert (lineage["source_first_line"], lineage["source_last_line"]) == (first, last)
    raw = bound_file(base, review["ci_source"]["raw_console"]).read_bytes()
    selected = raw[lineage["byte_start"]:lineage["byte_end_exclusive"]]
    assert hashlib.sha256(selected).hexdigest() == lineage["selected_raw_sha256"]
    assert selected.count(b"\n") == result["text"].count("\n") == last - first + 1
    assert result["text"].startswith("+ ./mvnw -B -U -V clean deploy\n")
    assert "+ ./mvnw site:site" not in result["text"]
    assert result["variant"]["task_argv"] == ["./mvnw", "-B", "-U", "-V", "clean", "install"]
    assert result["variant"]["removed_option"] is None
    assert result["variant"]["publication_comparable"] is False
    top, _ = ci_binding_inventory(result["text"])
    assert len(top) == top_count
    reviewed = [dict(row, disposition="excluded_declared_variant" if row["goal"] == "deploy:deploy" else "support", reason="fixture review") for row in top]
    projected = project_declared_variant_bindings(top, reviewed, result["variant"])
    assert len(projected) == len(top) - len({row["module"] for row in top})
    assert not any(row["goal"] == "deploy:deploy" for row in projected)
    assert all(row["line"] == top[row["ci_position"]]["line"] for row in projected)


@pytest.mark.parametrize("mutation", ["truncated_job", "missing_post", "wrong_timestamp", "duplicate_command", "extra_shell_command", "missing_success", "missing_footer", "wrong_stage"])
def test_transport_refuses_ambiguous_or_truncated_command_without_filtering(archive, mutation):
    review, base, _ = archive()
    raw = bound_file(base, review["ci_source"]["raw_console"]).read_bytes()
    original = ["./mvnw", "-B", "-U", "-V", "clean", "deploy"]
    if mutation == "truncated_job":
        raw = raw.removesuffix(b"Finished: SUCCESS\n")
    elif mutation == "missing_post":
        raw = raw.replace(b"Post stage\n", b"cut off\n", 1)
    elif mutation == "wrong_timestamp":
        raw = raw.replace(b"[2026-09-09T17:02:15.248Z] Apache Maven", b"[unknown] Apache Maven", 1)
    elif mutation == "duplicate_command":
        raw = b"[2026-09-09T17:00:00.000Z] + ./mvnw -B -U -V clean deploy\n" + raw
    elif mutation == "extra_shell_command":
        raw = raw.replace(b"[2026-09-09T17:02:15.248Z] Apache Maven", b"[2026-09-09T17:02:15.248Z] + mvn test\n[2026-09-09T17:02:15.248Z] Apache Maven", 1)
    elif mutation == "missing_success":
        raw = raw.replace(b"[INFO] BUILD SUCCESS", b"[INFO] BUILD FAILURE", 1)
    elif mutation == "missing_footer":
        raw = raw.replace(b"[INFO] Finished at: 2026-09-09T17:02:50Z", b"[INFO] missing footer", 1)
    else:
        raw = raw.replace(b"[Pipeline] { (Build)\n", b"[Pipeline] { (Other)\n", 1)
    with pytest.raises(ValueError):
        extract_timestamped_jenkins_command(raw, original)


@pytest.mark.parametrize("mutation", ["filtered_text", "changed_boundary", "changed_index", "replace_wrapper", "skip_tests", "jdk", "maven", "publication_claim"])
def test_review_cannot_rewrite_ci_identity_or_the_frozen_variant(archive, mutation):
    review, base, kwargs = archive()
    if mutation == "filtered_text":
        record = review["sources"]["official_ci"]
        path = bound_file(base, record)
        path.write_bytes(path.read_bytes().replace(b"[INFO] --- deploy:", b"[INFO] hidden-deploy:"))
        record.update(ref(path, base))
    elif mutation == "changed_boundary":
        review["ci_source"]["selection"]["source_first_line"] += 1
    elif mutation == "changed_index":
        record = review["ci_source"]["index"]
        path = bound_file(base, record)
        path.write_text(path.read_text().replace('"comparison_admitted": true', '"comparison_admitted": false'))
        record.update(ref(path, base))
    elif mutation == "replace_wrapper":
        kwargs["task"]["steps"][0]["argv"][0] = "mvn"
    elif mutation == "skip_tests":
        kwargs["task"]["steps"][0]["argv"].append("-DskipTests")
    elif mutation in {"jdk", "maven"}:
        kwargs["task"]["steps"][0]["java_major" if mutation == "jdk" else "maven_version"] = 21 if mutation == "jdk" else "3.9.9"
    else:
        review["declared_variant"]["publication_comparable"] = True
    with pytest.raises(ValueError):
        load_selected_ci_source(review, base, **kwargs)


@pytest.mark.parametrize("field,value", [
    ("result", "FAILURE"), ("building", True), ("url", "https://ci.example/other/1/"),
    ("commit", "f" * 40),
])
def test_independently_anchored_build_record_still_must_match_terminal_and_subject(archive, field, value):
    review, base, kwargs = archive()
    build_ref = review["ci_source"]["build"]
    build_path = bound_file(base, build_ref)
    original_hash = build_ref["sha256"]
    build = json.loads(build_path.read_text())
    if field == "commit":
        for action in build["actions"]:
            if action.get("lastBuiltRevision"):
                action["lastBuiltRevision"]["SHA1"] = value
    else:
        build[field] = value
    build_path.write_text(json.dumps(build))
    build_ref.update(ref(build_path, base))
    # This is a different, internally consistent synthetic frozen archive;
    # record binding alone still cannot turn a failed/wrong-subject run green.
    for path in (base / "frozen-index.json", base / review["ci_source"]["index"]["path"]):
        index = json.loads(path.read_text())
        next(e for e in index["evidence"] if e["sha256"] == original_hash).update(
            sha256=build_ref["sha256"], bytes=build_ref["bytes"]
        )
        path.write_text(json.dumps(index))
    kwargs["frozen_ci_index_ref"] = ref(base / "frozen-index.json", base)
    review["ci_source"]["index"].update(ref(base / review["ci_source"]["index"]["path"], base))
    with pytest.raises(ValueError, match="terminal, URL or commit differs"):
        load_selected_ci_source(review, base, **kwargs)


def test_tentacles_full_review_retains_both_jars_and_installed_pom(archive):
    review, base, kwargs = archive()
    draft = json.loads((base / "draft.json").read_text())
    draft["ci_alignment"]["archived_ci_index"] = kwargs["frozen_ci_index_ref"]
    result = apply_reviewed_plan(draft, kwargs["task"], base / "review.json", base)
    assert result["annotation_completeness"]["status"] == "complete"
    assert len(result["requirements"]) == 11
    plan = result["reviewed_execution_plan"]
    assert len(plan["ci_bindings"]) == 17
    assert len(plan["task_bindings"]) == 16
    assert len(plan["pom_bindings"]) == 20
    assert {"ci_original_console", "ci_build_record", "ci_frozen_index"} <= set(plan["sources"])
    for source in plan["sources"].values():
        assert bound_file(base, source).exists()
    package = [a for r in result["requirements"] if r["kind"] == "package" for a in r["expectations"]["artifacts"]]
    install = next(r for r in result["requirements"] if r["kind"] == "install")["expectations"]["artifacts"]
    assert {a["classifier"] for a in package} == {None, "jar-with-dependencies"}
    assert {(a["extension"], a["classifier"]) for a in install} == {("pom", None), ("jar", None), ("jar", "jar-with-dependencies")}
    assert not any("deploy:deploy" in r["validation"]["goals"] for r in result["requirements"])


def test_missing_attached_assembly_is_not_a_complete_install_scope(archive):
    review, base, kwargs = archive()
    draft = json.loads((base / "draft.json").read_text())
    draft["ci_alignment"]["archived_ci_index"] = kwargs["frozen_ci_index_ref"]
    next(r for r in review["requirements"] if r["kind"] == "install")["expectations"]["artifacts"].pop()
    (base / "review.json").write_text(json.dumps(review))
    with pytest.raises(ValueError, match="every CI repository write"):
        apply_reviewed_plan(draft, kwargs["task"], base / "review.json", base)
