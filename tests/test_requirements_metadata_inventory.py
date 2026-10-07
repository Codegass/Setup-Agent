"""Effective reactor defaults stay separate from reviewed task obligations."""
import hashlib

import pytest

from scripts.requirements_metadata_inventory import effective_reactor_inventory


def model(artifact, path, packaging="jar", plugin=""):
    return f'''<project><groupId>org.example</groupId><artifactId>{artifact}</artifactId>
    <version>1</version><packaging>{packaging}</packaging><build>
    <directory>/metadata/work/{path + '/' if path != '.' else ''}target</directory>
    <outputDirectory>/metadata/work/{path + '/' if path != '.' else ''}target/classes</outputDirectory>
    <finalName>{artifact}-1</finalName><plugins>{plugin}</plugins></build></project>'''


def inventory(raw, *, source=None, endpoint="install"):
    if isinstance(raw, str):
        raw = raw.encode()
    return effective_reactor_inventory(
        raw, workspace="/metadata/work", source_poms=source or {}, endpoint=endpoint,
        local_repository="/private-metadata-repo",
        provenance={"task_sha256": "a" * 64, "commit": "b" * 40,
                    "argv": ["mvn", "help:effective-pom"], "java_major": 17,
                    "maven_version": "3.9.16", "reactor_resolved": True,
                    "sha256": hashlib.sha256(raw).hexdigest()},
    )


def pom(a):
    return f'<project><artifactId>{a}</artifactId></project>'.encode()


def test_reactor_maps_pinned_modules_and_never_assigns_parent_a_jar():
    result = inventory("<projects>" + model("parent", ".", "pom") + model("child", "child") + "</projects>",
                       source={"pom.xml": pom("parent"), "child/pom.xml": pom("child")})
    parent, child = result["models"]
    assert result["model_count"] == 2
    assert result["models_requiring_manual_review"] == 0
    assert parent["artifacts"] == []
    assert [a["extension"] for a in parent["installs"]] == ["pom"]
    assert child["artifacts"][0]["path"] == "child/target/child-1.jar"
    assert child["classes_directory"] == "child/target/classes"
    assert parent["module_path"] == "." and child["module_path"] == "child"
    assert result["default_artifact_declarations"] == 4
    assert "not proof" in result["scope"]


def test_test_endpoint_cannot_acquire_packaging_or_install_obligation():
    row = inventory(model("child", "child"), source={"child/pom.xml": pom("child")}, endpoint="test")["models"][0]
    assert row["artifacts"] == row["installs"] == []


@pytest.mark.parametrize("source", [{}, {"child/pom.xml": pom("different-module")}])
def test_missing_or_mismatched_pinned_pom_cannot_resolve_module_mapping(source):
    row = inventory(model("child", "child"), source=source)["models"][0]
    assert row["resolution_status"] == "unresolved"
    assert row["mapping_errors"]
    assert row["artifacts"][0]["path"].startswith("/metadata/")


def test_custom_or_escaped_output_is_a_review_exception():
    for path in ("/metadata/work/child/dist", "/metadata/work/../outside/target", "/other/target"):
        raw = model("child", "child").replace("/metadata/work/child/target</directory>", path + "</directory>")
        row = inventory(raw, source={"child/pom.xml": pom("child")})["models"][0]
        assert "custom_build_directory_needs_explicit_module_mapping" in row["exceptions"]


def test_shade_is_a_manual_exception_not_an_automatic_main_jar_claim():
    plugin = "<plugin><artifactId>maven-shade-plugin</artifactId></plugin>"
    row = inventory(model("child", "child", plugin=plugin), source={"child/pom.xml": pom("child")})["models"][0]
    assert row["resolution_status"] == "unresolved"
    assert "artifact_plugin_requires_review:maven-shade-plugin" in row["exceptions"]


def test_model_and_raw_document_hashes_both_preserved():
    raw = model("child", "child").encode()
    result = inventory(raw, source={"child/pom.xml": pom("child")})
    row = result["models"][0]
    assert row["provenance"]["source_reactor_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["provenance"]["sha256"] != result["source_sha256"]


def test_duplicate_module_names_do_not_silently_merge_reactor_scope():
    result = inventory("<projects>" + model("same", "a") + model("same", "b") + "</projects>",
                       source={"a/pom.xml": pom("same"), "b/pom.xml": pom("same")})
    assert result["model_count"] == 2
    assert result["models"][1]["resolution_status"] == "unresolved"


def test_green_ci_with_retry_errors_can_be_inventoried_without_certifying_tests():
    from scripts.build_benchmark_requirements import ci_binding_inventory
    from sag.benchmark.native_evidence import maven_events
    raw = ('[INFO] --- surefire:3.5.3:test (default-test) @ child ---\n'
           '[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n'
           '[WARNING] flakyTest Run 2: PASS\n'
           '[INFO] BUILD SUCCESS\n')
    top, nested = ci_binding_inventory(raw)
    assert len(top) == 1 and top[0]['goal'] == 'surefire:test' and not nested
    assert maven_events(raw, terminal=True, serial=True)[0]['status'] == 'unavailable'
    with pytest.raises(ValueError, match='one full successful'):
        ci_binding_inventory('[INFO] BUILD FAILURE\n' + raw)


def test_draft_known_passes_cannot_certify_a_whole_stage_or_absence():
    from sag.benchmark.requirements import summarize
    spec = {"annotation_completeness": {"status": "review_required", "gaps": ["inherited_checks_unreviewed"]},
            "requirements": [{"id": "compile", "kind": "compile", "depends_on": []}]}
    score = summarize(spec, [{"id": "compile", "status": "passed"}], [], [])
    assert score["status"] == "unavailable"
    assert score["requirements"][0]["status"] == "passed"
    assert all(status == "unavailable" for status in score["groups"].values())
    failed = summarize(spec, [{"id": "compile", "status": "failed"}], [], [])
    assert failed["status"] == failed["groups"]["build"] == "incomplete"


def test_known_supplement_adds_bound_categories_but_cannot_complete_or_change_task(tmp_path):
    import json
    from scripts.requirements_metadata_inventory import append_known_requirements
    from sag.benchmark.requirements import canonical_digest, normalize_task
    task = normalize_task({"repo": "org/example", "sha": "b" * 40,
                           "steps": [{"id": "build", "runner": "gradle", "argv": ["./gradlew", "build"]}]})
    spec = {"schema_version": 2, "policy_version": "ci-requirements-v2", "project_id": "example",
            "task_sha256": canonical_digest(task), "repository": task["repo"], "commit": task["sha"],
            "annotation_completeness": {"status": "review_required", "gaps": ["artifacts_unreviewed"]},
            "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
            "requirements": [{"id": "pending", "step_id": "build", "kind": "unclassified",
                              "scope": {"status": "unresolved"}, "validation": {"rule": "unclassified"}}]}
    source = tmp_path / 'build.gradle'; source.write_text('build.dependsOn javadoc\n')
    review = {"project_id": "example", "repository": task["repo"], "commit": task["sha"],
              "task_sha256": spec["task_sha256"], "annotation_completeness": "review_required",
              "requirements": [{"id": "docs", "step_id": "build", "kind": "documentation",
                "scope": {"status": "unresolved"}, "validation": {"rule": "native_goal", "tasks": [":javadoc"]},
                "sources": [{"path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}]}
    path = tmp_path / 'review.json'; path.write_text(json.dumps(review))
    result = append_known_requirements(spec, task, path, tmp_path / 'out')
    assert result['annotation_completeness'] == spec['annotation_completeness']
    assert result['requirements'][0] == spec['requirements'][0]
    assert result['group_labels'] == ['documentation']
    assert result['migration']['new_categories_not_expressible_by_legacy_stages'] == ['documentation']
    assert len(spec['requirements']) == 1
    review['task_sha256'] = 'c' * 64; path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match='same incomplete task'):
        append_known_requirements(spec, task, path, tmp_path / 'out')
    review['task_sha256'] = spec['task_sha256']; path.write_text(json.dumps(review))
    source.write_text('changed')
    with pytest.raises(ValueError, match='byte hash mismatch'):
        append_known_requirements(spec, task, path, tmp_path / 'out')
