import hashlib
import json
from pathlib import Path

import pytest

from sag.agent.java_success_certificates import evaluate_java_success_manifest

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "java_success_certificates"
    / "d2r6-five-project-inputs.json"
)
OUTPUT_FIXTURE = FIXTURE.with_name("d2r6-five-project-certificates.json")
SOURCE_CHECKSUMS = FIXTURE.with_name("d2r6-five-project-source-checksums.json")
REPOSITORY_ROOT = Path(__file__).parents[1]


def test_five_project_manifest_has_the_expected_shadow_results():
    result = evaluate_java_success_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))
    certificates = {item["project_id"]: item for item in result["certificates"]}

    assert set(certificates) == {
        "tomcat-jakartaee-migration",
        "commons-dbcp",
        "ignite",
        "jackrabbit",
        "cassandra-java-driver",
    }
    assert certificates["tomcat-jakartaee-migration"]["result"] == "repository_green"
    assert certificates["commons-dbcp"]["result"] == "repository_product_test_green"
    assert certificates["ignite"]["result"] == "incomplete"
    assert certificates["jackrabbit"]["result"] == "incomplete"
    assert certificates["cassandra-java-driver"]["result"] == "build_failed"
    assert certificates["ignite"]["test_targets"]["required"] == 28
    assert certificates["ignite"]["test_targets"]["satisfied"] == 0
    assert (
        certificates["ignite"]["test_targets"]["basis_ref"]
        == "candidate:trunk:build_recommendation.test_modules"
    )
    assert certificates["cassandra-java-driver"]["test_steps"]["failed"] == 0
    assert certificates["cassandra-java-driver"]["test_steps"]["missing"] == 1
    assert certificates["cassandra-java-driver"]["test_steps"]["required_ids"] == [
        "plan:test:0:root-verify:test-jdk-17"
    ]
    assert certificates["cassandra-java-driver"]["test_steps"]["unexpected_ids"] == [
        "runtime:test:root-verify:test-jdk-8"
    ]
    assert certificates["cassandra-java-driver"]["build_units"]["closure_fraction"] == "4/8"
    assert all(item["flags"]["green_proof_verified"] is False for item in certificates.values())
    assert all(item["assurance_level"] == "legacy_projected" for item in certificates.values())


def test_checked_in_five_project_output_is_reproducible():
    result = evaluate_java_success_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert result == json.loads(OUTPUT_FIXTURE.read_text(encoding="utf-8"))


def test_source_checksum_snapshot_is_subject_bound_and_locally_auditable():
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"]
    input_subjects = {
        item["scope"]["subject"]["project_id"]: item["scope"]["subject"]["run_id"]
        for item in inputs
    }
    manifest = json.loads(SOURCE_CHECKSUMS.read_text(encoding="utf-8"))
    manifest_subjects = {item["project_id"]: item["run_id"] for item in manifest["projects"]}

    assert manifest["schema_version"] == 1
    assert manifest["raw_sources_gitignored"] is True
    assert manifest_subjects == input_subjects
    for project in manifest["projects"]:
        assert project["sources"]
        for source in project["sources"]:
            assert len(source["sha256"]) == 64
            raw_path = REPOSITORY_ROOT / source["path"]
            if raw_path.is_file():
                assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == source["sha256"]


def test_manifest_rejects_duplicate_projects():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["inputs"].append(payload["inputs"][0])

    with pytest.raises(ValueError, match="duplicate project ids"):
        evaluate_java_success_manifest(payload)
