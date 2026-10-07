"""Archived Jenkins node transport and predeclared publication exclusion tests."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_benchmark_requirements import ci_binding_inventory
from scripts.requirements_ci_sources import (
    extract_jenkins_console,
    load_selected_ci_source,
    project_declared_variant_bindings,
)


def file_ref(path, base):
    raw = path.read_bytes()
    return {"path": str(path.relative_to(base)), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


@pytest.fixture
def archived_node(tmp_path):
    fixture = Path(__file__).parent / "fixtures/requirements_reviewed_sling_metadata.zip"

    def load(name="sling-commons-mime"):
        base = tmp_path / name
        base.mkdir()
        with ZipFile(fixture) as archive:
            prefix = name + "/"
            review = json.loads(archive.read(prefix + "review.json"))
            task = json.loads(archive.read(prefix + "task.json"))
            refs = {r["path"]: r for group in (review["sources"], review["additional_sources"]) for r in group.values()}
            for relative in refs:
                path = base / relative
                assert path.resolve().is_relative_to(base.resolve())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(prefix + relative))
            index_path = base / "independently-frozen-index.json"
            index_path.write_bytes(archive.read(prefix + "frozen-index.json"))
        index = json.loads(index_path.read_text())
        kwargs = {"frozen_ci_index_ref": file_ref(index_path, base), "frozen_ci_index_base": base,
                  "task": task, "selected_url": index["selected_url"]}
        return review, base, kwargs
    return load


@pytest.mark.parametrize("name,original_count,pom_count", [
    ("sling-commons-mime", 30, 31), ("sling-commons-osgi", 31, 32),
])
def test_selected_node_preserves_raw_source_and_only_excludes_declared_publication(archived_node, name, original_count, pom_count):
    review, base, kwargs = archived_node(name)
    result = load_selected_ci_source(review, base, **kwargs)
    actual, nested = ci_binding_inventory(result["text"])
    projected = project_declared_variant_bindings(actual, review["ci_bindings"], result["variant"])
    assert len(actual) == original_count
    assert len(projected) == original_count - 1
    assert len(nested) == 7
    assert len(review["pom_bindings"]) == pom_count
    assert len(review["requirements"]) == 19
    assert result["variant"]["publication_comparable"] is False
    assert len(result["source_paths"]) == 3
    assert result["lineage"]["original_html_sha256"] == review["ci_source"]["raw_html"]["sha256"]
    assert not any(row["goal"] == "deploy:deploy" for row in projected)
    assert any(row["goal"] == "deploy:deploy" for row in actual)
    javadoc = projected[-1]
    assert javadoc["goal"] == "javadoc:javadoc"
    assert javadoc["ci_position"] == actual[-1]["position"]
    assert javadoc["position"] == original_count - 2
    assert javadoc["line"] == actual[-1]["line"]
    assert javadoc["occurrence"] == actual[-1]["occurrence"]
    integration = next(row for row in projected if row["goal"] == "jacoco:report-integration")
    assert integration["disposition"] == "ci_disabled"
    install = next(row for row in review["requirements"] if row["kind"] == "install")
    assert [(a["extension"], a["classifier"]) for a in install["expectations"]["artifacts"]] == [("pom", None), ("jar", None), ("jar", "sources")]


def test_html_transport_keeps_all_native_text_and_decodes_entities():
    raw = b'<html>outside<pre class="console-output">first\n<span>--- &lt;x&gt;</span>\nlast</pre>outside</html>'
    assert extract_jenkins_console(raw) == "first\n--- <x>\nlast"


@pytest.mark.parametrize("raw", [
    b"<pre>not a console</pre>",
    b'<pre class="console-output">unclosed',
    b'<pre class="console-output">a</pre><pre class="console-output">b</pre>',
    b'<pre class="console-output"><pre>nested</pre></pre>',
    b'<pre class="console-output"><script>hidden</script></pre>',
])
def test_html_transport_refuses_ambiguous_or_incomplete_container(raw):
    with pytest.raises(ValueError):
        extract_jenkins_console(raw)


@pytest.mark.parametrize("mutation,reason", [
    ("filtered_text", "complete HTML console payload"),
    ("self_asserted_index", "independently frozen index"),
    ("node_url", "node URL differs"),
    ("selected_run", "run differs"),
    ("new_skip_tests", "changes beyond"),
    ("missing_variant", "preexisting frozen declaration"),
    ("claim_publication", "differs from its frozen declaration"),
    ("runtime", "runtime differs"),
])
def test_review_cannot_replace_lineage_or_expand_declared_variant(archived_node, mutation, reason):
    review, base, kwargs = archived_node()
    if mutation == "filtered_text":
        ref = review["sources"]["official_ci"]
        path = base / ref["path"]
        path.write_text(path.read_text().replace("[INFO] --- deploy:", "[INFO] removed-deploy:"))
        ref.update(file_ref(path, base))
    elif mutation == "self_asserted_index":
        ref = review["ci_source"]["index"]
        path = base / ref["path"]
        value = json.loads(path.read_text())
        value["notes"].append("new convenient assertion")
        path.write_text(json.dumps(value))
        ref.update(file_ref(path, base))
    elif mutation == "node_url":
        review["ci_source"]["selected_node_url"] += "other"
    elif mutation == "selected_run":
        kwargs["selected_url"] += "other/"
    elif mutation == "new_skip_tests":
        kwargs["task"]["steps"][0]["argv"].append("-DskipTests")
    elif mutation == "missing_variant":
        review.pop("declared_variant")
    elif mutation == "claim_publication":
        review["declared_variant"]["publication_comparable"] = True
    else:
        kwargs["task"]["steps"][0]["java_major"] = 21
    with pytest.raises(ValueError, match=reason):
        load_selected_ci_source(review, base, **kwargs)


def test_variant_is_not_authorized_by_new_review_alone(archived_node):
    review, base, kwargs = archived_node()
    frozen_path = base / kwargs["frozen_ci_index_ref"]["path"]
    value = json.loads(frozen_path.read_text())
    value["notes"] = []
    frozen_path.write_text(json.dumps(value))
    kwargs["frozen_ci_index_ref"] = file_ref(frozen_path, base)
    review_index = base / review["ci_source"]["index"]["path"]
    review_index.write_bytes(frozen_path.read_bytes())
    review["ci_source"]["index"].update(file_ref(review_index, base))
    with pytest.raises(ValueError, match="preexisting frozen declaration"):
        load_selected_ci_source(review, base, **kwargs)


@pytest.mark.parametrize("mutation,reason", [
    ("drop_goal", "Every original CI occurrence"),
    ("exclude_test", "exactly its default deploy"),
    ("change_position", "Projected task order"),
    ("absent_variant", "requires a verified"),
])
def test_projection_preserves_obligations_and_requires_verified_variant(archived_node, mutation, reason):
    review, base, kwargs = archived_node()
    result = load_selected_ci_source(review, base, **kwargs)
    actual, _ = ci_binding_inventory(result["text"])
    if mutation == "drop_goal":
        review["ci_bindings"].pop()
    elif mutation == "exclude_test":
        next(row for row in review["ci_bindings"] if row["goal"] == "surefire:test")["disposition"] = "excluded_declared_variant"
    elif mutation == "change_position":
        review["ci_bindings"][-1]["task_position"] = 999
    else:
        result["variant"] = None
    with pytest.raises(ValueError, match=reason):
        project_declared_variant_bindings(actual, review["ci_bindings"], result["variant"])


@pytest.mark.parametrize("name,javadoc_position", [("sling-commons-mime", 28), ("sling-commons-osgi", 29)])
def test_full_review_import_preserves_artifact_sources_and_projects_javadoc_position(archived_node, name, javadoc_position):
    from scripts.build_benchmark_requirements import apply_reviewed_plan
    from sag.benchmark.requirements import bound_file

    review, base, kwargs = archived_node(name)
    fixture = Path(__file__).parent / "fixtures/requirements_reviewed_sling_metadata.zip"
    with ZipFile(fixture) as archive:
        draft = json.loads(archive.read(name + "/draft.json"))
    # This anchor comes independently from the frozen-package fixture, just as
    # build_project archives ci/<project>/index.json before reading the review.
    draft["ci_alignment"]["archived_ci_index"] = kwargs["frozen_ci_index_ref"]
    path = base / "review.json"
    path.write_text(json.dumps(review))
    result = apply_reviewed_plan(draft, kwargs["task"], path, base)
    assert result["annotation_completeness"]["status"] == "complete"
    assert len(result["requirements"]) == 19
    plan = result["reviewed_execution_plan"]
    for source in plan["sources"].values():
        assert bound_file(base, source).is_file()
    assert {"ci_original_html", "ci_selected_stage", "ci_frozen_index", "official_ci"} <= set(plan["sources"])
    assert plan["sources"]["ci_original_html"]["sha256"] == review["ci_source"]["raw_html"]["sha256"]
    assert len(plan["nested_ci_bindings"]) == 7
    assert any(b["goal"] == "deploy:deploy" for b in plan["ci_bindings"])
    assert not any(b["goal"] == "deploy:deploy" for b in plan["task_bindings"])
    assert not any("deploy:deploy" in r["validation"]["goals"] for r in result["requirements"])
    assert result["ci_alignment"]["declared_variant"]["publication_comparable"] is False
    javadoc = next(r for r in result["requirements"] if r["kind"] == "documentation")
    original_javadoc = plan["ci_bindings"][-1]
    assert javadoc["validation"]["position"] == javadoc_position
    assert javadoc["validation"]["position"] + 1 == original_javadoc["position"]
    assert javadoc["sources"][-1]["line"] == original_javadoc["line"]
    assert javadoc["validation"]["occurrence"] == original_javadoc["occurrence"]
    packaged = [a for r in result["requirements"] if r["kind"] == "package" for a in r["expectations"]["artifacts"]]
    installed = next(r for r in result["requirements"] if r["kind"] == "install")["expectations"]["artifacts"]
    assert len(packaged) == 2
    assert len(installed) == 3
    assert {a["classifier"] for a in packaged} == {None, "sources"}
    assert all(a["evidence_tier"] == "declared" for a in packaged + installed)
    assert all(len(r["sources"]) == 3 for r in result["requirements"])


def test_full_review_import_refuses_missing_attached_source_jar(archived_node):
    from scripts.build_benchmark_requirements import apply_reviewed_plan

    review, base, kwargs = archived_node()
    fixture = Path(__file__).parent / "fixtures/requirements_reviewed_sling_metadata.zip"
    with ZipFile(fixture) as archive:
        draft = json.loads(archive.read("sling-commons-mime/draft.json"))
    draft["ci_alignment"]["archived_ci_index"] = kwargs["frozen_ci_index_ref"]
    next(r for r in review["requirements"] if r["kind"] == "install")["expectations"]["artifacts"].pop()
    path = base / "review.json"
    path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="every CI repository write"):
        apply_reviewed_plan(draft, kwargs["task"], path, base)
