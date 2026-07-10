"""Verifier honesty layer for the execution-strategy fixes (spec §4).

jdk_mismatch: required JDK != active at validation time -> PARTIAL cap.
reactor_scope_narrowed: tests ran in a strict subset of test-bearing modules.
Both are report-only; they NEVER block execution."""

import json

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.module_metrics import assemble_module_metrics


class ConflictOrch:
    """Minimal fake: manifest + java -version + benign answers elsewhere."""

    def __init__(self, java="11", manifest=None):
        self.java = java
        self.manifest = manifest or {}

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def test_collect_jdk_conflict_on_mismatch():
    # _collect_env_conflicts is the renamed _collect_jdk_conflicts (it now
    # also covers python); the jdk_mismatch contract is unchanged.
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java="11", manifest={"java_version": "17"})
    assert validator._collect_env_conflicts() == ["jdk_mismatch"]


def test_no_conflict_when_matching_or_unknown():
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java="17", manifest={"java_version": "17"})
    assert validator._collect_env_conflicts() == []
    validator.docker_orchestrator = ConflictOrch(java="11", manifest={})  # no requirement
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
