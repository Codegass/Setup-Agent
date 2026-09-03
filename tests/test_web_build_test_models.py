from sag.web.models import BuildSummary, SourceScopeSummary, TestSummary


def test_build_summary_new_fields_default_and_serialize():
    b = BuildSummary(
        state="success",
        system="maven",
        class_count=115,
        source_scope=SourceScopeSummary(
            covered=36,
            total=36,
            availability="available",
            basis="sealed physical build success over validated full module scope",
            evidence_refs=["output_source_scope"],
        ),
        jar_count=0,
        module_output_count=3,
        artifact_samples=["target/classes/Foo.class"],
        warnings=["wrapper jar excluded"],
        evidence_refs=["output_x"],
    )
    dumped = b.model_dump(mode="json", by_alias=True)
    assert dumped["system"] == "maven"
    assert dumped["classCount"] == 115
    assert dumped["sourceScope"] == {
        "covered": 36,
        "total": 36,
        "availability": "available",
        "basis": "sealed physical build success over validated full module scope",
        "reason": None,
        "evidenceRefs": ["output_source_scope"],
    }
    assert dumped["jarCount"] == 0
    assert dumped["moduleOutputCount"] == 3
    assert dumped["artifactSamples"] == ["target/classes/Foo.class"]
    assert dumped["warnings"] == ["wrapper jar excluded"]
    assert dumped["evidenceRefs"] == ["output_x"]


def test_build_summary_defaults_are_empty_not_fake():
    dumped = BuildSummary().model_dump(mode="json", by_alias=True)
    assert dumped["system"] is None
    assert dumped["classCount"] is None
    assert dumped["sourceScope"] is None
    assert dumped["jarCount"] is None
    assert dumped["artifactSamples"] == []
    assert dumped["warnings"] == []
    assert dumped["evidenceRefs"] == []


def test_build_summary_accepts_snake_case_source_scope_projection():
    dumped = BuildSummary.model_validate(
        {
            "source_scope": {
                "covered": None,
                "total": 36,
                "availability": "unavailable",
                "basis": None,
                "reason": "build source scope was not sealed",
                "evidence_refs": ["output_build"],
            }
        }
    ).model_dump(mode="json", by_alias=True)

    assert dumped["sourceScope"] == {
        "covered": None,
        "total": 36,
        "availability": "unavailable",
        "basis": None,
        "reason": "build source scope was not sealed",
        "evidenceRefs": ["output_build"],
    }


def test_test_summary_new_fields_serialize():
    t = TestSummary(
        state="partial", pass_count=18805, fail_count=5, skip_count=29, total=18839,
        pass_rate=99.8, errors=0, report_file_count=760,
        unique_total=9497, unique_passed=9480, unique_failed=5,
        unique_errors=0, unique_skipped=12,
        declared_total=20500, method_execution_rate=46.3,
        failing_names=["com.x.FooTest.testA"], conflicts=["test_report_parse_error"],
        evidence_refs=["output_5b9a"],
    )
    d = t.model_dump(mode="json", by_alias=True)
    assert d["total"] == 18839 and d["pass"] == 18805 and d["fail"] == 5
    assert d["errors"] == 0
    assert d["reportFileCount"] == 760
    assert d["uniqueTotal"] == 9497 and d["uniquePassed"] == 9480
    assert d["uniqueFailed"] == 5 and d["uniqueErrors"] == 0 and d["uniqueSkipped"] == 12
    assert d["declaredTotal"] == 20500
    assert d["methodExecutionRate"] == 46.3
    assert d["failingNames"] == ["com.x.FooTest.testA"]
    assert d["conflicts"] == ["test_report_parse_error"]
    assert d["evidenceRefs"] == ["output_5b9a"]


def test_test_summary_defaults_no_fake_metrics():
    d = TestSummary().model_dump(mode="json", by_alias=True)
    assert d["uniqueTotal"] is None
    assert d["declaredTotal"] is None
    assert d["methodExecutionRate"] is None
    assert d["reportFileCount"] is None
    assert d["failingNames"] == [] and d["conflicts"] == [] and d["evidenceRefs"] == []
