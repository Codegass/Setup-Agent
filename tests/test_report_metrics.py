from sag.tools.report_metrics import assemble_report_metrics


def _snapshot():
    return {
        "status": {
            "overall": "partial",
            "tests_total": 18839, "tests_passed": 18805, "tests_failed": 5,
            "tests_errors": 0, "tests_skipped": 29, "pass_pct": 99.8,
            "tests_unique": 9497, "tests_passed_unique": 9480, "tests_failed_unique": 5,
            "tests_errors_unique": 0, "tests_skipped_unique": 12,
            "static_test_count": 20500, "execution_rate": 46.3,
        },
        "physical_evidence": {"class_files": 115, "jar_files": 0},
    }


def test_assemble_maps_runner_and_unique_metrics():
    m = assemble_report_metrics(
        snapshot=_snapshot(),
        build_evidence={"build_system": "maven", "tool": "/usr/bin/mvn",
                        "artifact_samples": ["target/classes/Foo.class"],
                        "module_output_count": 3, "warnings": []},
        test_analysis={"failing_test_names": ["com.x.FooTest.testA"], "report_file_count": 760},
        conflicts=["test_report_parse_error"],
        evidence_refs=["output_5b9a"],
        generated_at="2026-06-15T12:00:00",
    )
    assert m["version"] == 1
    assert m["build"]["system"] == "maven"
    assert m["build"]["class_count"] == 115 and m["build"]["jar_count"] == 0
    assert m["build"]["module_output_count"] == 3
    assert m["build"]["artifact_samples"] == ["target/classes/Foo.class"]
    assert m["test"]["total"] == 18839 and m["test"]["passed"] == 18805
    assert m["test"]["failed"] == 5 and m["test"]["skipped"] == 29
    assert m["test"]["pass_rate"] == 99.8
    assert m["test"]["report_file_count"] == 760
    assert m["test"]["unique_total"] == 9497 and m["test"]["unique_passed"] == 9480
    assert m["test"]["declared_total"] == 20500
    assert m["test"]["method_execution_rate"] == 46.3
    assert m["test"]["failing_names"] == ["com.x.FooTest.testA"]
    assert m["test"]["conflicts"] == ["test_report_parse_error"]
    assert m["test"]["evidence_refs"] == ["output_5b9a"]


def test_assemble_handles_missing_evidence_with_nulls():
    m = assemble_report_metrics(
        snapshot={"status": {}, "physical_evidence": {}},
        build_evidence={}, test_analysis={}, conflicts=[], evidence_refs=[],
        generated_at="2026-06-15T12:00:00",
    )
    assert m["build"]["class_count"] is None and m["build"]["artifact_samples"] == []
    assert m["test"]["total"] is None and m["test"]["unique_total"] is None
    assert m["test"]["failing_names"] == []


def test_assemble_truncates_failing_and_samples():
    m = assemble_report_metrics(
        snapshot={"status": {}, "physical_evidence": {}},
        build_evidence={"artifact_samples": [f"a{i}.class" for i in range(50)]},
        test_analysis={"failing_test_names": [f"T{i}" for i in range(500)]},
        conflicts=[], evidence_refs=[], generated_at="x",
    )
    assert len(m["build"]["artifact_samples"]) == 10
    assert len(m["test"]["failing_names"]) == 50


def test_assemble_surfaces_runtime_model_and_iteration_counts():
    m = assemble_report_metrics(
        snapshot=_snapshot(),
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-06-18T00:00:00",
        execution_metrics={
            "model": "claude-sonnet-4.5",
            "total_iterations": 6,
            "max_iterations": 40,
        },
    )
    assert m["model"] == "claude-sonnet-4.5"
    assert m["total_iterations"] == 6
    assert m["max_iterations"] == 40


def test_assemble_runtime_keys_default_none_without_execution_metrics():
    m = assemble_report_metrics(
        snapshot={"status": {}, "physical_evidence": {}},
        build_evidence={}, test_analysis={}, conflicts=[], evidence_refs=[],
        generated_at="x",
    )
    assert m["model"] is None
    assert m["total_iterations"] is None
    assert m["max_iterations"] is None


def test_build_dict_surfaces_time_command_artifact():
    metrics = assemble_report_metrics(
        snapshot={"phases": {"build": True}, "status": {"overall": "success"}},
        build_evidence={
            "build_system": "maven", "tool": "Maven 3.9.6",
            "class_files": 115, "jar_files": 1,
            "build_time": "47.2s", "build_command": "clean package",
            "artifact": "target/commons-cli-1.6.0.jar",
            "artifact_samples": ["target/commons-cli-1.6.0.jar"],
        },
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-06-17T00:00:00",
    )
    build = metrics["build"]
    assert build["time"] == "47.2s"
    assert build["note"] == "clean package"
    assert build["artifact"] == "target/commons-cli-1.6.0.jar"
