from sag.tools.module_metrics import assemble_module_metrics, MODULE_METRICS_VERSION


def _scan():
    return [
        {"path": "connect/api", "name": "connect:api", "class_count": 180,
         "jar_count": 3, "report_dirs": ["/w/connect/api/target/surefire-reports"]},
        {"path": "connect/runtime", "name": "connect:runtime", "class_count": 0,
         "jar_count": 0, "report_dirs": []},
        {"path": "raft", "name": "raft", "class_count": 0, "jar_count": 0, "report_dirs": []},
    ]


def test_reconciles_reactor_status_and_tests():
    metrics = assemble_module_metrics(
        modules=_scan(),
        reactor_status={"connect:api": "success", "connect:runtime": "failure",
                        "raft": "skipped"},
        tests={"connect/api": {"tests_total": 198, "tests_passed": 198, "tests_failed": 0,
                               "tests_errors": 0, "tests_skipped": 0,
                               "failing_names": [], "failing_count": 0,
                               "evidence_refs": ["/w/connect/api/target/surefire-reports"]}},
        build_systems=["maven"],
        build_error_samples={"connect/runtime": ["[ERROR] cannot find symbol"]},
        generated_at="2026-06-15 00:00:00",
    )
    assert metrics["version"] == MODULE_METRICS_VERSION
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["connect/api"]["build_status"] == "success"
    assert by_path["connect/api"]["build_source"] == "reactor"
    assert by_path["connect/api"]["tests_passed"] == 198
    assert by_path["connect/runtime"]["build_status"] == "failure"
    assert by_path["connect/runtime"]["build_error_samples"] == ["[ERROR] cannot find symbol"]
    assert by_path["raft"]["build_status"] == "skipped"
    s = metrics["module_summary"]
    assert s["modules_total"] == 3 and s["modules_failed"] == 1 and s["modules_skipped"] == 1
    assert s["modules_built"] == 1 and s["single_module"] is False


def test_falls_back_to_artifacts_when_no_reactor():
    metrics = assemble_module_metrics(
        modules=[{"path": "core", "name": "core", "class_count": 50, "jar_count": 1,
                  "report_dirs": []}],
        reactor_status={},
        tests={},
        build_systems=["gradle"],
        build_error_samples={},
        generated_at="t",
    )
    m = metrics["modules"][0]
    assert m["build_status"] == "success"   # artifacts present
    assert m["build_source"] == "artifacts"
    assert metrics["module_summary"]["single_module"] is True


def test_no_reactor_jar_without_classes_is_not_built():
    # commons-vfs shape: no reactor summary, a module left a stale jar but
    # compiled no fresh .class files (its build failed dependency resolution).
    # It must read detected-but-not-built, not an optimistic "success".
    metrics = assemble_module_metrics(
        modules=[
            {"path": "core", "name": "core", "class_count": 12, "jar_count": 1,
             "report_dirs": []},
            {"path": "examples", "name": "examples", "class_count": 0, "jar_count": 1,
             "report_dirs": []},
        ],
        reactor_status={},
        tests={},
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["core"]["build_status"] == "success"        # has classes
    assert by_path["examples"]["build_status"] != "success"    # jar only -> not built
    assert metrics["module_summary"]["modules_total"] == 2
    assert metrics["module_summary"]["modules_built"] == 1


def test_reactor_matches_descriptive_maven_name_label():
    # Real Maven reactor labels use the module <name> display string, e.g.
    # "Apache Kafka :: Connect :: API", while scan_modules derives the key from
    # the directory path: name="connect:api", path="connect/api". The assembler
    # must reconcile the descriptive label with the path-derived module so the
    # reactor status (and its build_source) is not dropped.
    metrics = assemble_module_metrics(
        modules=[
            {"path": "connect/api", "name": "connect:api", "class_count": 0,
             "jar_count": 0, "report_dirs": []},
            {"path": "connect/runtime", "name": "connect:runtime", "class_count": 0,
             "jar_count": 0, "report_dirs": []},
        ],
        reactor_status={
            "Apache Kafka :: Connect :: API": "success",
            "Apache Kafka :: Connect :: Runtime": "failure",
        },
        tests={},
        build_systems=["maven"],
        build_error_samples={"connect/runtime": ["[ERROR] cannot find symbol"]},
        generated_at="t",
    )
    by_path = {m["path"]: m for m in metrics["modules"]}
    # Without robust matching these fall back to "unknown"/"skipped" with
    # build_source "none"/"partial" and the reactor failure/error samples are lost.
    assert by_path["connect/api"]["build_status"] == "success"
    assert by_path["connect/api"]["build_source"] == "partial"  # success but no artifacts
    assert by_path["connect/runtime"]["build_status"] == "failure"
    assert by_path["connect/runtime"]["build_source"] == "reactor"
    assert by_path["connect/runtime"]["build_error_samples"] == ["[ERROR] cannot find symbol"]
    s = metrics["module_summary"]
    assert s["modules_failed"] == 1


def test_failing_names_capped_but_count_exact():
    names = [f"com.x.T{i}.m" for i in range(600)]
    metrics = assemble_module_metrics(
        modules=[{"path": "m", "name": "m", "class_count": 1, "jar_count": 0,
                  "report_dirs": ["/w/m"]}],
        reactor_status={"m": "success"},
        tests={"m": {"tests_total": 600, "tests_passed": 0, "tests_failed": 600,
                     "tests_errors": 0, "tests_skipped": 0,
                     "failing_names": names, "failing_count": 600,
                     "evidence_refs": ["/w/m"]}},
        build_systems=["maven"], build_error_samples={}, generated_at="t",
    )
    m = metrics["modules"][0]
    assert len(m["failing_names"]) == 500
    assert m["failing_count"] == 600
    assert metrics["module_summary"]["modules_with_test_failures"] == 1
