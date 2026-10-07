"""Metadata preparation must not silently relax or invent task obligations."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_benchmark_requirements import (
    build_bundle,
    build_project,
    digest,
    import_effective_pom,
    import_request,
    lifecycle_endpoint,
    maven_goals,
    observed_checks,
    skip_tests,
    task_digest,
)
from sag.agent.acceptance_task import AcceptanceTask
from sag.benchmark.requirements import validate_requirements


@pytest.mark.parametrize("kind", ["file", "missing"])
def test_preparation_must_be_a_directory_before_any_export(tmp_path, kind):
    preparation = tmp_path / "status-manifest.json"
    if kind == "file":
        preparation.write_text("{}")
    with pytest.raises(ValueError, match="root directory"):
        build_bundle(tmp_path / "manifest.json", tmp_path / "out", tmp_path,
                     preparation=preparation)
    assert not (tmp_path / "out").exists()


def effective(packaging="jar", plugins="", final_name="library-1", directory="target"):
    return f"""<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
      <groupId>org.example</groupId><artifactId>library</artifactId><version>1</version>
      {f'<packaging>{packaging}</packaging>' if packaging else ''}
      <build><finalName>{final_name}</finalName><directory>{directory}</directory>
      <outputDirectory>target/classes</outputDirectory><plugins>{plugins}</plugins></build></project>""".encode()


def import_pom(raw, endpoint="install", **kwargs):
    provenance = {
        "task_sha256": "a" * 64,
        "commit": "b" * 40,
        "argv": ["mvn", "help:effective-pom"],
        "java_major": 17,
        "maven_version": "3.9.16",
        "reactor_resolved": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return import_effective_pom(
        raw,
        module=".",
        endpoint=endpoint,
        local_repository="/metadata/repository",
        provenance=provenance,
        **kwargs,
    )


@pytest.mark.parametrize(
    "packaging,extension", [("jar", "jar"), ("", "jar"), ("war", "war"), ("bundle", "jar")]
)
def test_declared_main_artifact_and_install_coordinates(packaging, extension):
    result = import_pom(effective(packaging), bundle_mapping_confirmed=True)
    assert result["resolution_status"] == "declared"
    assert result["artifacts"] == [
        {
            "role": "main",
            "module": ".",
            "path": f"target/library-1.{extension}",
            "extension": extension,
            "classifier": None,
            "coordinates": {"group_id": "org.example", "artifact_id": "library", "version": "1"},
            "evidence_tier": "declared",
        }
    ]
    assert {x["extension"] for x in result["installs"]} == {"pom", extension}
    assert (
        result["installs"][0]["repository_relative_path"] == "org/example/library/1/library-1.pom"
    )
    assert result["classes_directory"] == "target/classes"


def test_pom_module_has_no_main_jar_but_installs_pom():
    result = import_pom(effective("pom"))
    assert result["artifacts"] == []
    assert [a["role"] for a in result["installs"]] == ["installed_pom"]


def test_test_endpoint_cannot_acquire_jar_requirement():
    result = import_pom(effective(), endpoint="test")
    assert result["artifacts"] == result["installs"] == []


@pytest.mark.parametrize(
    "plugin,reason",
    [
        (
            "<plugin><artifactId>maven-jar-plugin</artifactId><configuration><skipIfEmpty>true</skipIfEmpty></configuration></plugin>",
            "skip_if_empty",
        ),
        ("<plugin><artifactId>maven-shade-plugin</artifactId></plugin>", "artifact_plugin"),
        (
            "<plugin><artifactId>maven-jar-plugin</artifactId><configuration><classifier>special</classifier></configuration></plugin>",
            "main_classifier",
        ),
        (
            "<plugin><artifactId>custom-plugin</artifactId><executions><execution><phase>package</phase></execution></executions></plugin>",
            "custom_artifact_binding",
        ),
    ],
)
def test_artifact_exceptions_require_review(plugin, reason):
    result = import_pom(effective(plugins=plugin))
    assert result["resolution_status"] == "unresolved"
    assert any(reason in gap for gap in result["exceptions"])


def test_test_jar_classifier_does_not_replace_main_role():
    plugin = """<plugin><artifactId>maven-jar-plugin</artifactId><executions><execution>
      <goals><goal>test-jar</goal></goals><configuration><classifier>tests</classifier></configuration>
      </execution></executions></plugin>"""
    result = import_pom(effective(plugins=plugin))
    assert result["resolution_status"] == "declared"
    assert result["artifacts"][0]["classifier"] is None


def test_bundle_mapping_and_absolute_metadata_paths_cannot_be_guessed():
    assert "bundle_extension_mapping_unconfirmed" in import_pom(effective("bundle"))["exceptions"]
    assert (
        "absolute_metadata_workspace_path_requires_mapping"
        in import_pom(effective(directory="/tmp/meta/target"))["exceptions"]
    )


def test_unresolved_properties_and_missing_provenance_are_not_declared():
    raw = effective(final_name="${something}")
    result = import_effective_pom(
        raw, module=".", endpoint="install", local_repository=None, provenance={}
    )
    assert result["resolution_status"] == "unresolved"
    assert "artifact_output_path_unresolved" in result["exceptions"]
    assert "local_repository_unresolved" in result["exceptions"]
    assert "effective_pom_hash_mismatch" in result["exceptions"]


def test_argv_parsing_preserves_goal_order_without_treating_options_as_goals():
    argv = [
        "./mvnw",
        "-f",
        "pom.xml",
        "-pl",
        "!a,!b",
        "-Pfoo",
        "clean",
        "verify",
        "javadoc:javadoc",
        "checkstyle:check",
    ]
    assert maven_goals(argv) == ["clean", "verify", "javadoc:javadoc", "checkstyle:check"]
    assert lifecycle_endpoint(maven_goals(argv)) == "verify"
    assert skip_tests(["mvn", "install", "-DskipTests"])
    assert not skip_tests(["mvn", "install", "-DskipTests=false"])


def test_positive_check_observations_do_not_turn_report_into_coverage_threshold():
    raw = """[INFO] --- enforcer:3.6.3:enforce (enforce-java-version) @ example ---
      [INFO] --- jacoco:0.8.15:report (report) @ example ---
      [INFO] --- jacoco:0.8.15:check (check) @ example ---
      [INFO] --- maven-checkstyle-plugin:3.1.0:check (check) @ example ---"""
    assert [r["goal"] for r in observed_checks(raw)] == [
        "enforcer:enforce",
        "jacoco:check",
        "checkstyle:check",
    ]


def project_fixture(tmp_path: Path, *, goals=None, default=None, runner="maven"):
    bundle = tmp_path / "v1"
    bundle.mkdir()
    task = {
        "schema_version": 1,
        "repo": "apache/example",
        "sha": "b" * 40,
        "steps": [
            {
                "id": "ci-step-1",
                "runner": runner,
                "argv": ["mvn", *(goals or [])],
                "cwd": ".",
                "java_major": 17,
            }
        ],
    }
    task_file = bundle / "task.json"
    task_file.write_text(json.dumps(task, indent=2))
    source = bundle / "source-pom.xml"
    source.write_text(
        "<project><build><defaultGoal>" + (default or "") + "</defaultGoal></build></project>"
    )
    p = {
        "id": "example",
        "repo": task["repo"],
        "commit": task["sha"],
        "scope_note": "test fixture",
        "task": {"path": "task.json", "sha256": hashlib.sha256(task_file.read_bytes()).hexdigest()},
        "steps": [{**task["steps"][0], "stages": ["build", "test"]}],
        "ci": {
            "selected_url": None,
            "selected_cell": None,
            "comparison_admitted": False,
            "modules": [],
            "evidence_status": "unavailable",
            "evidence": [
                {"path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
            ],
        },
    }
    return p, bundle, task


@pytest.mark.parametrize(
    "goals",
    [
        "clean verify apache-rat:check japicmp:cmp spotbugs:check pmd:check pmd:cpd-check javadoc:javadoc checkstyle:check",
        "clean verify apache-rat:check japicmp:cmp checkstyle:check spotbugs:check pmd:check pmd:cpd-check javadoc:javadoc",
    ],
)
def test_default_goal_exact_order_preserved_and_stages_cannot_hide_checks(tmp_path, goals):
    p, bundle, task = project_fixture(tmp_path, default=goals)
    spec = build_project(p, bundle, tmp_path / "v2", tmp_path)
    assert spec["steps"][0]["effective_goals"] == goals.split()
    assert {"documentation", "quality_check", "compile", "package", "test"} <= set(
        spec["group_labels"]
    )
    assert spec["annotation_completeness"]["status"] == "review_required"
    assert any(r["kind"] == "unclassified" for r in spec["requirements"])
    validate_requirements(spec, AcceptanceTask.model_validate(task))


def test_no_package_requirement_for_test_only_and_unknown_goal_retained(tmp_path):
    p, bundle, _ = project_fixture(tmp_path, goals=["test", "custom:inspect"])
    spec = build_project(p, bundle, tmp_path / "v2", tmp_path)
    assert "package" not in spec["group_labels"]
    assert any(
        r["kind"] == "unclassified" and r["subtype"] == "custom:inspect"
        for r in spec["requirements"]
    )


def test_missing_effective_pom_does_not_invent_artifacts_or_campaign_readiness(tmp_path):
    p, bundle, _ = project_fixture(tmp_path, goals=["install", "-DskipTests"])
    path = bundle / "manifest.json"
    path.write_text(json.dumps({"projects": [p]}))
    before = path.read_bytes()
    index = build_bundle(path, tmp_path / "v2", tmp_path)
    assert not index["campaign_ready"]
    spec = json.loads((tmp_path / "v2/requirements/example.json").read_text())
    assert not any(r["kind"] == "test" for r in spec["requirements"])
    artifact_req = next(r for r in spec["requirements"] if r["kind"] == "package")
    assert artifact_req["expectations"]["status"] == "unresolved"
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="overwrite"):
        build_bundle(path, bundle, tmp_path)


def test_task_hash_matches_sag_serializer_defaults(tmp_path):
    _, _, task = project_fixture(tmp_path, goals=["test"])
    task["steps"][0].pop("java_major")
    task["steps"][0].pop("cwd")
    assert task_digest(task) == AcceptanceTask.model_validate(task).sha256
    task["steps"][0]["maven_version"] = "3.9.16"
    assert task_digest(task) == AcceptanceTask.model_validate(task).sha256


def test_import_cli_request_archives_bytes_and_does_not_run_recorded_command(tmp_path):
    raw = effective()
    (tmp_path / "effective.xml").write_bytes(raw)
    request = {
        "pom_path": "effective.xml",
        "module": ".",
        "lifecycle_endpoint": "package",
        "local_repository": None,
        "provenance": {
            "task_sha256": "a" * 64,
            "commit": "b" * 40,
            "argv": ["do-not-execute"],
            "java_major": 17,
            "maven_version": "3.9.16",
            "reactor_resolved": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    result = import_request(path, tmp_path / "import")
    assert result["resolution_status"] == "declared"
    assert (tmp_path / "import" / result["source"]["path"]).read_bytes() == raw


@pytest.fixture
def archived_review(tmp_path):
    """Real frozen sources only; no local output dependency, downloads or Maven."""
    import zipfile
    fixture = Path(__file__).parent / "fixtures/requirements_reviewed_metadata.zip"

    def load(project_id):
        destination = tmp_path / project_id
        destination.mkdir()
        with zipfile.ZipFile(fixture) as archive:
            prefix = project_id + "/"
            draft = json.loads(archive.read(prefix + "draft.json"))
            task = json.loads(archive.read(prefix + "task.json"))
            review = json.loads(archive.read(prefix + "review.json"))
            for source in review["sources"].values():
                target = destination / source["path"]
                assert target.resolve().is_relative_to(destination.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(prefix + source["path"]))
        path = destination / "review.json"
        path.write_text(json.dumps(review))
        return draft, task, review, path, destination
    return load


@pytest.mark.parametrize("name,count", [("commons-dbutils", 8), ("commons-net", 18), ("commons-csv", 23), ("commons-dbcp", 23)])
def test_reviewed_archives_resolve_every_requirement_and_native_occurrence(archived_review, name, count):
    from scripts.build_benchmark_requirements import apply_reviewed_plan
    draft, task, review, path, destination = archived_review(name)
    result = apply_reviewed_plan(draft, task, path, destination)
    assert result["annotation_completeness"]["status"] == "complete"
    assert len(result["requirements"]) == count
    assert all(row["validation"]["plan_resolved"] and row["dependencies_complete"] for row in result["requirements"])
    assert all(row["scope"]["module_path"] == "." for row in result["requirements"])
    if name == "commons-dbutils":
        assert "package" not in result["group_labels"]
        assert "install" not in result["group_labels"]
        report = next(row for row in result["requirements"] if row["subtype"] == "coverage_report_generation")
        assert report["validation"]["goals"] == ["jacoco:report"]
        assert not any(row["subtype"] == "coverage_threshold" for row in result["requirements"])
    elif name == "commons-net":
        install = next(row for row in result["requirements"] if row["kind"] == "install")
        assert len(install["expectations"]["artifacts"]) == 10
        assert {a["extension"] for a in install["expectations"]["artifacts"]} == {"pom", "jar", "xml", "json", "spdx.json"}
    else:
        plan = result["reviewed_execution_plan"]
        assert len(plan["ci_bindings"]) == 32
        assert len(plan["nested_ci_bindings"]) == (9 if name == "commons-csv" else 32)
        assert "documentation" in result["group_labels"]
        assert "install" not in result["group_labels"]
        assert plan["selected_goals"] == review["selected_goals"]


@pytest.mark.parametrize("mutation,message", [
    ("missing_ci", "Every ordered CI occurrence"),
    ("missing_pom", "Every effective POM execution"),
    ("duplicate_occurrence", "Every ordered CI occurrence"),
    ("task", "identity differs"),
    ("source_hash", "byte hash mismatch"),
    ("support_check", "mandatory outcome"),
    ("missing_install", "every CI repository write"),
    ("artifact_coordinate", "coordinates differ"),
    ("artifact_path", "Package outputs"),
    ("dependencies", "dependencies need explicit review"),
])
def test_reviewed_plan_rejects_omitted_or_conflicting_scope(archived_review, mutation, message):
    from scripts.build_benchmark_requirements import apply_reviewed_plan
    draft, task, review, path, destination = archived_review("commons-net")
    if mutation == "missing_ci":
        review["ci_bindings"].pop()
    elif mutation == "missing_pom":
        review["pom_bindings"].pop()
    elif mutation == "duplicate_occurrence":
        review["ci_bindings"][1]["occurrence"] = 1
    elif mutation == "task":
        review["task_sha256"] = "f" * 64
    elif mutation == "source_hash":
        review["sources"]["runtime"]["sha256"] = "f" * 64
    elif mutation == "support_check":
        review["ci_bindings"][1].update(disposition="support", requirement_id=None)
    elif mutation == "missing_install":
        next(r for r in review["requirements"] if r["kind"] == "install")["expectations"]["artifacts"].pop()
    elif mutation == "artifact_coordinate":
        next(r for r in review["requirements"] if r["kind"] == "package")["expectations"]["artifacts"][0]["coordinates"]["version"] = "2"
    elif mutation == "artifact_path":
        next(r for r in review["requirements"] if r["subtype"] == "source_jar")["expectations"]["artifacts"][0]["path"] = "target/wrong.jar"
    elif mutation == "dependencies":
        review["requirements"][0]["dependencies_complete"] = False
    path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match=message):
        apply_reviewed_plan(draft, task, path, destination)


def test_reviewed_occurrences_count_each_execution_independently():
    from scripts.build_benchmark_requirements import ci_execution_bindings
    log = "\n".join([
        "[INFO] --- enforcer:1:enforce (java) @ p ---",
        "[INFO] --- enforcer:1:enforce (maven) @ p ---",
        "[INFO] --- enforcer:1:enforce (java) @ p ---",
        "[INFO] BUILD SUCCESS",
    ])
    assert [row["occurrence"] for row in ci_execution_bindings(log)] == [0, 0, 1]
    with pytest.raises(ValueError, match="Forked"):
        ci_execution_bindings("[INFO] >>> fork\n" + log)


def test_reviewed_archive_goals_are_consumable_by_the_shared_native_reader(archived_review):
    from scripts.build_benchmark_requirements import apply_reviewed_plan, ci_native_text
    from sag.benchmark.native_evidence import maven_events
    for name in ("commons-dbutils", "commons-net", "commons-csv", "commons-dbcp"):
        draft, task, review, path, destination = archived_review(name)
        result = apply_reviewed_plan(draft, task, path, destination)
        ci = destination / result["reviewed_execution_plan"]["sources"]["official_ci"]["path"]
        events = maven_events(ci_native_text(ci.read_text()), terminal=True, serial=True)
        for row in result["requirements"]:
            validation = row["validation"]
            matches = [event for event in events if event["module"] == row["module"]
                       and event["goal"] in validation["goals"]
                       and event["execution"] == validation["execution"]
                       and event["occurrence"] == validation["occurrence"]]
            assert len(matches) == 1, row["id"]
            assert matches[0]["status"] == "passed", row["id"]


@pytest.mark.parametrize("mutation", ["missing_nested", "nested_substitute", "default_order"])
def test_default_goal_and_fork_review_cannot_drop_or_replace_obligations(archived_review, mutation):
    from scripts.build_benchmark_requirements import apply_reviewed_plan
    draft, task, review, path, destination = archived_review("commons-csv")
    if mutation == "missing_nested":
        review["nested_ci_bindings"].pop()
    elif mutation == "nested_substitute":
        review["nested_ci_bindings"][0].update(disposition="requirement", requirement_id=review["requirements"][0]["id"])
    else:
        review["selected_goals"][-2:] = reversed(review["selected_goals"][-2:])
    path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="nested CI|Default goal order"):
        apply_reviewed_plan(draft, task, path, destination)


def test_timestamped_fork_preserves_source_lines_and_top_level_occurrences():
    from scripts.build_benchmark_requirements import ci_binding_inventory
    raw = "\n".join([
        "2026-09-07T01:02:03.0000000Z [INFO] --- compiler:1:compile (default) @ p ---",
        "2026-09-07T01:02:03.0000000Z [INFO] >>> javadoc:1:javadoc (default-cli) > compile @ p >>>",
        "2026-09-07T01:02:03.0000000Z [INFO] --- compiler:1:compile (default) @ p ---",
        "2026-09-07T01:02:03.0000000Z [INFO] <<< javadoc:1:javadoc (default-cli) < compile @ p <<<",
        "2026-09-07T01:02:03.0000000Z [INFO] --- javadoc:1:javadoc (default-cli) @ p ---",
        "2026-09-07T01:02:03.0000000Z [INFO] BUILD SUCCESS",
    ])
    top, nested = ci_binding_inventory(raw)
    assert [(x["line"], x["occurrence"]) for x in top] == [(1, 0), (5, 0)]
    assert nested[0]["line"] == 3
    assert nested[0]["goal"] == "compiler:compile"
    with pytest.raises(ValueError, match="Forked Maven framing"):
        ci_binding_inventory(raw.replace("< compile", "< test"))


def test_review_import_reference_identity_is_stable_for_relative_cli_path(archived_review, monkeypatch):
    from scripts.build_benchmark_requirements import apply_reviewed_plan
    draft, task, review, path, destination = archived_review("commons-dbutils")
    absolute = apply_reviewed_plan(draft, task, path, destination)
    monkeypatch.chdir(path.parent)
    relative = apply_reviewed_plan(draft, task, Path("review.json"), destination)
    assert digest(absolute) == digest(relative)
