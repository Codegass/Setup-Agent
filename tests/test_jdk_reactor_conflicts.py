"""Verifier honesty layer for the execution-strategy fixes (spec §4).

jdk_mismatch: required JDK != active at validation time -> PARTIAL cap.
reactor_scope_narrowed: tests ran in a strict subset of test-bearing modules.
Both are report-only; they NEVER block execution."""

import pytest
from sag.agent.control_events import canonical_json
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    publish_evidence_revision,
)
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.module_metrics import assemble_module_metrics


class ConflictOrch:
    """Minimal fake: manifest + java -version + benign answers elsewhere."""

    def __init__(self, java="11", manifest=None):
        self.java = java
        self.manifest = manifest or {}
        publication = publish_evidence_revision(
            self,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=canonical_json(self.manifest).encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        assert publication.published

    def read_file(self, path):
        if path == REQUIREMENTS_PATH:
            return {
                "success": True,
                "exit_code": 0,
                "content": canonical_json(self.manifest),
            }
        return None

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in cmd and REQUIREMENTS_PATH in cmd:
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(
                    [("build-requirements.json", canonical_json(self.manifest))]
                ),
            }
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": canonical_json(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def test_collect_jdk_conflict_on_mismatch():
    # _collect_env_conflicts is the renamed _collect_jdk_conflicts (it now
    # also covers python); the jdk_mismatch contract is unchanged.
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java="11", manifest={"java_version": "17"})
    assert validator._collect_env_conflicts() == ["jdk_mismatch"]


@pytest.mark.parametrize(
    ("java", "manifest"),
    [
        ("17", {"java_version": "17"}),
        ("11", {}),
    ],
)
def test_no_conflict_when_matching_or_unknown(java, manifest):
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java=java, manifest=manifest)
    assert validator._collect_env_conflicts() == []


def _metrics(tested_pairs):
    """tested_pairs: list of (path, has_test_sources, tests_total)."""
    return assemble_module_metrics(
        modules=[
            {"path": p, "name": p, "class_count": 5, "jar_count": 1,
             "report_dirs": [], "has_test_sources": bearing}
            for p, bearing, _ in tested_pairs
        ],
        reactor_status={p: "success" for p, _, _ in tested_pairs},
        tests={
            p: {"tests_total": total, "tests_passed": total, "failing_count": 0}
            for p, _, total in tested_pairs if total
        },
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )


def test_summary_counts_test_bearing_modules():
    metrics = _metrics([("api", True, 10), ("core", True, 0), ("docs", False, 0)])
    s = metrics["module_summary"]
    assert s["modules_test_bearing"] == 2
    assert s["modules_tested"] == 1


def test_scope_narrowed_condition():
    # The report emits reactor_scope_narrowed when 0 < tested < test_bearing.
    s = _metrics([("api", True, 10), ("core", True, 0)])["module_summary"]
    assert 0 < s["modules_tested"] < s["modules_test_bearing"]  # narrow -> conflict fires
    s_full = _metrics([("api", True, 10), ("core", True, 3)])["module_summary"]
    assert s_full["modules_tested"] == s_full["modules_test_bearing"]  # full -> no conflict
