import subprocess

from sag.agent.module_coverage import coverage_conflicts
from sag.agent.physical_validator import PhysicalValidator
from sag.tools.module_metrics import assemble_module_metrics


class LocalTree:
    def execute_command(self, command, **kwargs):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": result.stdout,
        }


def test_gradle_root_buildsrc_and_custom_source_sets_share_explicit_identity(tmp_path):
    for path, content in {
        "build.gradle.kts": "plugins { java }",
        "settings.gradle.kts": 'include("native-app")',
        "engine/src/main/java/Engine.java": "class Engine {}",
        "engine/src/test/java/EngineTest.java": "class EngineTest {}",
        "buildSrc/build.gradle.kts": "plugins { `kotlin-dsl` }",
        "buildSrc/src/main/kotlin/Build.kt": "class Build",
        "native-app/build.gradle.kts": "plugins { java }",
        "native-app/src/main/java/Main.java": "class Main {}",
        "native-app/src/test/java/MainTest.java": "class MainTest {}",
    }.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    validator = PhysicalValidator(docker_orchestrator=LocalTree())
    expectations = validator._parse_gradle_expected_artifacts(str(tmp_path))
    assert {item["module"] for item in expectations} == {":root", "buildSrc", "native-app"}
    # Expectations exist BEFORE output, and compiler languages use build/classes.
    assert [item["min_count"] for item in expectations if item["type"] == "classes"] == [1, 1, 1]
    assert all(
        item["path"].endswith("build/classes") for item in expectations if item["type"] == "classes"
    )
    scope = validator._scope_expectations_to_attempted(
        expectations, (":root", "buildSrc", "native-app")
    )
    assert scope.conflict is None
    assert scope.denominator_modules == (":root", "buildSrc", "native-app")
    scan = {item["path"]: item for item in validator.scan_modules(str(tmp_path), "gradle")}
    assert scan["."]["has_test_sources"]
    assert not scan["."].get("aggregator_shell")
    assert not scan["buildSrc"]["has_test_sources"]
    assert str(tmp_path / "native-app/src/test/java") not in scan["."]["source_roots"]
    # Missing scope remains missing: never silently match a similarly named project.
    unknown = validator._scope_expectations_to_attempted(
        expectations, (":root", "buildSrc", "nativeapp")
    )
    assert unknown.conflict == "build_coverage_scope_unverified"


def test_observed_tests_prove_test_bearing_even_without_conventional_source_dir():
    coverage = assemble_module_metrics(
        modules=[
            {"path": ".", "has_test_sources": False, "class_count": 10},
            {"path": "buildSrc", "has_test_sources": True, "class_count": 3},
        ],
        reactor_status={},
        tests={".": {"tests_total": 20}, "buildSrc": {"tests_total": 2}},
        build_systems=["gradle"],
        build_error_samples={},
        generated_at="now",
    )
    assert (
        coverage["module_summary"]["modules_tested"]
        == coverage["module_summary"]["modules_test_bearing"]
        == 2
    )
    assert coverage["modules"][0]["has_test_sources"] is False
    assert coverage["modules"][0]["test_bearing_evidence"] == ["runner_xml"]
    # Old or malformed persisted counts are a conflict, never a >100% rate.
    coverage["module_summary"]["modules_test_bearing"] = 1
    assert "module_coverage_count_conflict" in coverage_conflicts(
        {"summary": coverage["module_summary"]}
    )
