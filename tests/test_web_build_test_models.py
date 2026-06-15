from sag.web.models import BuildSummary, TestSummary


def test_build_summary_new_fields_default_and_serialize():
    b = BuildSummary(
        state="success",
        system="maven",
        class_count=115,
        jar_count=0,
        module_output_count=3,
        artifact_samples=["target/classes/Foo.class"],
        warnings=["wrapper jar excluded"],
        evidence_refs=["output_x"],
    )
    dumped = b.model_dump(mode="json", by_alias=True)
    assert dumped["system"] == "maven"
    assert dumped["classCount"] == 115
    assert dumped["jarCount"] == 0
    assert dumped["moduleOutputCount"] == 3
    assert dumped["artifactSamples"] == ["target/classes/Foo.class"]
    assert dumped["warnings"] == ["wrapper jar excluded"]
    assert dumped["evidenceRefs"] == ["output_x"]


def test_build_summary_defaults_are_empty_not_fake():
    dumped = BuildSummary().model_dump(mode="json", by_alias=True)
    assert dumped["system"] is None
    assert dumped["classCount"] is None
    assert dumped["jarCount"] is None
    assert dumped["artifactSamples"] == []
    assert dumped["warnings"] == []
    assert dumped["evidenceRefs"] == []
