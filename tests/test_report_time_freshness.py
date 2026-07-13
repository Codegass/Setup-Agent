"""Report-time artifact counts must bypass the mid-run TTL cache.

Live bigtop evidence (session 20260713_014403): the container held 162 real
.class files at report time (6 from the early Maven module build + ~156 from a
later Gradle test compile), but the report header said
"Build: ✅ 6 classes, 1 JARs".

Root cause: PhysicalValidator._check_class_files (and _check_build_artifacts_
complete) cache their find-count with a 60s TTL. The count was taken early —
right after the Maven module built, before Gradle compiled anything — and the
cached 6 survived to report generation ~4 minutes later. Meanwhile scan_modules
counted fresh, so the per-module breakdown showed the Gradle modules as built:
the header and the breakdown contradicted each other.

Fix contract:
- Report-time validation must never serve stale artifact counts: the final
  validation pass forces freshness (clears the validator cache) so the header
  reflects the container's real state at report time.
- Mid-run caching stays: two consecutive checks within the TTL during the run
  still hit the cache (agent iterations must not hammer docker).
"""

import pytest

from sag.agent.physical_validator import PhysicalValidator


class ScriptedClassCountOrchestrator:
    """Container whose .class count grows between the early check and report time.

    The early Maven module build leaves 6 .class files; a later Gradle test
    compile brings the total to 162. ``advance_to_report_time()`` flips the
    count the container reports, modeling the ~4 minutes of build activity
    between the early validation and report generation.

    Every ``find ... -name '*.class' ... | wc -l`` command returns the CURRENT
    count. The find-invocation counter lets the regression test assert the
    mid-run cache still collapses repeated checks to a single ``find``.
    """

    EARLY_CLASS_COUNT = 6
    REPORT_CLASS_COUNT = 162

    def __init__(self):
        self._class_count = self.EARLY_CLASS_COUNT
        self.class_find_count = 0  # count-style `find ... *.class ... wc -l`
        self.commands = []

    def advance_to_report_time(self):
        """Simulate the Gradle test compile completing after the early check."""
        self._class_count = self.REPORT_CLASS_COUNT

    def execute_command(self, command):
        self.commands.append(command)
        c = command.strip()

        is_class_find = "-name '*.class'" in c and "find " in c
        if is_class_find and "| wc -l" in c:
            self.class_find_count += 1
            return {"exit_code": 0, "output": str(self._class_count)}
        if is_class_find:
            # Path listing: emit that many distinct class paths.
            paths = "\n".join(
                f"/workspace/bigtop/mod/target/classes/C{i}.class"
                for i in range(self._class_count)
            )
            return {"exit_code": 0, "output": paths}

        # JAR count: one real build jar throughout the run.
        if "-name '*.jar'" in c and "| wc -l" in c:
            return {"exit_code": 0, "output": "1"}
        if "-name '*.jar'" in c:
            return {"exit_code": 0, "output": "/workspace/bigtop/mod/target/mod.jar"}

        # Everything else (node_modules probes, package.json, recency stat,
        # missing-class scans) is irrelevant to the count contract.
        return {"exit_code": 0, "output": ""}


def _make_validator(orch):
    return PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")


def test_report_time_class_count_bypasses_stale_early_cache():
    """The report-time validation must report 162, not the cached early 6.

    Sequence mirrors the live run:
      1. Early check (right after the Maven module build) sees 6 and caches it.
      2. ~4 min of Gradle test compilation lands 156 more classes (162 total).
      3. Report generation runs the final validation pass.

    Buggy behavior: the 60s TTL is long-since valid, so the report path serves
    the cached 6 -> header says "6 classes" while the real container has 162.
    Fixed behavior: the report path forces freshness -> 162.
    """
    orch = ScriptedClassCountOrchestrator()
    validator = _make_validator(orch)

    # (1) Early mid-run check caches 6.
    early = validator.validate_build_artifacts(project_name="bigtop")
    assert early["class_files"] == 6

    # (2) Gradle test compile completes after the early check.
    orch.advance_to_report_time()

    # (3) Report-time validation pass: must NOT serve the stale 6.
    report = validator.validate_build_artifacts_fresh(project_name="bigtop")

    assert report["class_files"] == 162, (
        "report-time validation served a stale cached count "
        f"({report['class_files']}) instead of the container's real 162"
    )


def test_report_time_freshness_helper_clears_all_artifact_caches():
    """validate_build_status must also see fresh counts at report time.

    Both the header (validate_build_artifacts -> _check_class_files) and the
    build verdict (validate_build_status -> _check_build_artifacts_complete)
    depend on cached counts under different cache keys. A single freshness pass
    must invalidate both so they never disagree with scan_modules.
    """
    orch = ScriptedClassCountOrchestrator()
    validator = _make_validator(orch)

    # Warm BOTH caches at the early count.
    validator.validate_build_artifacts(project_name="bigtop")
    early_status = validator.validate_build_status("bigtop")
    assert early_status["evidence"]["artifact_count"] >= 6

    orch.advance_to_report_time()

    # Fresh report pass must reflect the 162 classes in the build evidence too.
    fresh = validator.validate_build_artifacts_fresh(project_name="bigtop")
    assert fresh["class_files"] == 162

    status_after = validator.validate_build_status("bigtop")
    assert status_after["evidence"]["artifact_count"] >= 162


def test_two_consecutive_midrun_checks_hit_cache_and_run_find_once():
    """Regression: mid-run caching must survive the fix.

    Two immediate consecutive class-file checks within the TTL must execute the
    counting ``find`` exactly once — the second is served from cache so agent
    iterations don't hammer docker.
    """
    orch = ScriptedClassCountOrchestrator()
    validator = _make_validator(orch)

    first = validator._check_class_files("/workspace/bigtop")
    second = validator._check_class_files("/workspace/bigtop")

    assert first["count"] == 6
    assert second["count"] == 6
    assert orch.class_find_count == 1, (
        "mid-run cache regressed: the counting find ran "
        f"{orch.class_find_count} times for two consecutive checks (expected 1)"
    )
