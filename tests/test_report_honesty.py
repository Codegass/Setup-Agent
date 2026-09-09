# tests/test_report_honesty.py
"""Report-layer honesty (SAG v2 Plan 3, Task 3).

Two P2 findings carried since the original failure analysis:

A. A run whose sealed verdict was ``failed`` still printed
   ``### Blockers (0)`` / ``✅ No blocking issues``. The setup-mode adapter
   (``_build_report_snapshot``) maps EVERY sealed conflict onto an ``INFO``
   attention item, so the blockers list was empty by construction no matter
   how badly the run failed. The report must derive blocker lines from the
   sealed evidence itself: the verdict outcome, the failing phase, and that
   phase's recorded failure signature.

B. The former recommendations section printed generic ecosystem prose
   (``pip install -e . && pytest`` / ``mvn clean test -DskipTests=false``)
   that contradicted the coordinates the survey actually recorded. The report
   may identify surveyed text as an observation, but it must not turn either
   surveyed or inferred data into a next command. When a survey source is
   reachable but recorded nothing, the report names that evidence gap.

C. (SAG v2 Plan 4, Task 3 — 2026-07-26 post-acceptance audit) The TVM
   report presented 28 pytest *collection* error nodes plus their 28 paired
   skip nodes as "56 tests executed", never quoted the structured
   ``RuntimeError`` that caused every collection to fail, and never named
   the scope (``full``, 11,702 collected) of the attempt it was describing.
   When the sealed evidence carries ``collection_errors``, the test section
   must state that collection failed and how many tests actually executed,
   quote ``collection_error_summary`` verbatim as the root cause, derive a
   blocker from it, and name the latest attempt's scope and command.
"""

import json
import re

from build_requirements_fakes import complete_python_build_requirements_v1
from container_evidence_fakes import ContainerFS, add_published_mutable_json

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.report_tool import ReportTool

# ---------------------------------------------------------------------------
# House fixtures
# ---------------------------------------------------------------------------


class FakeOrch(ContainerFS):
    """In-memory container FS (house pattern from tests/test_build_preflight.py)."""

    def __init__(self, manifest=None):
        super().__init__()
        if manifest is not None:
            add_published_mutable_json(
                self,
                self,
                path=REQUIREMENTS_PATH,
                record_kind="build_requirements",
                record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
                payload=manifest,
            )

    def execute_command(self, cmd, workdir=None):
        del workdir
        return self(cmd)


class _Trunk:
    def __init__(self, environment_summary):
        self.environment_summary = environment_summary


class FakeContextManager:
    def __init__(self, environment_summary=None):
        self._trunk = _Trunk(environment_summary or {})

    def load_trunk_context(self):
        return self._trunk


def _tool(*, docker_orchestrator=None, context_manager=None):
    """A render-only ReportTool (no orchestrator work beyond the fakes)."""
    tool = ReportTool.__new__(ReportTool)
    tool.docker_orchestrator = docker_orchestrator
    tool.context_manager = context_manager
    tool.physical_validator = None
    return tool


def _sealed_snapshot(
    *,
    verdict,
    conflicts=(),
    build_green=True,
    build_judgment="success",
    phase_records=(),
    build_system="python",
    attention_raw=None,
    rates=None,
    failed=0,
    errors=0,
):
    """A setup-mode snapshot shaped exactly like ``_build_report_snapshot``."""
    canonical = {
        "verdict": verdict,
        "build_evidence": {
            "observed": True,
            "green": build_green,
            "judgment": build_judgment,
            "outcome": "success" if build_green else "failed",
            "refs": ["obs:build:1"],
        },
        "conflicts": list(conflicts),
        "phase_records": [dict(record) for record in phase_records],
        "rates": rates or {},
    }
    raw = attention_raw
    if raw is None:
        # The adapter's actual behaviour: every conflict lands as INFO.
        raw = [{"severity": "INFO", "icon": "INFO", "message": conflict} for conflict in conflicts]
    status = {"verdict": verdict, "overall": verdict}
    if rates is not None or failed or errors:
        status.update(
            {
                "tests_total": 100,
                "tests_passed": 100 - failed - errors,
                "tests_failed": failed,
                "tests_errors": errors,
                "tests_skipped": 0,
            }
        )
    return {
        "mode": "setup",
        "status": status,
        "project": {"type": "Python", "build_system": build_system},
        "phases": {"clone": True, "build": build_green, "test": None},
        "physical_evidence": {"build_system": build_system},
        "test_history": {},
        "per_module": {},
        "flags": {},
        "evidence_result": {"status": verdict, "conflicts": list(conflicts)},
        "attention": {"items": [], "raw": raw, "ignored_lines": 0},
        "canonical_snapshot": canonical,
        **({"rates": rates} if rates is not None else {}),
    }


def test_setup_report_leads_with_rate_lines_and_derives_the_word_once():
    rates = {
        "build": {
            "modules": {
                "rate": 100.0,
                "band": "fully",
                "numerator": 14,
                "denominator": 14,
            },
            "classes": {
                "rate": 99.6,
                "band": "most",
                "numerator": 3400,
                "denominator": 3412,
            },
        },
        "test": {
            "cases": {
                "rate": 100.0,
                "band": "most",
                "numerator": 100,
                "denominator": 100,
            },
            "modules": {
                "rate": 50.0,
                "band": "half",
                "numerator": 1,
                "denominator": 2,
            },
        },
        "coverage": {
            "status": "collected",
            "line_rate": 55.5,
            "source": "jacoco-injected",
        },
    }
    snapshot = _sealed_snapshot(
        verdict="partial",
        conflicts=("metrics_conflict",),
        rates=rates,
        build_system="maven",
    )
    snapshot["canonical_snapshot"]["build_evidence"].update(
        {
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "source_files": 36,
            "compiled_classes": 56,
        }
    )
    snapshot["canonical_snapshot"]["phase_records"] = [
        {
            "phase": "build",
            "termination": "completed",
            "validated_outcome": "success",
        }
    ]
    snapshot["canonical_snapshot"]["test_stats"] = {
        "discovered": 468,
        "unique": {
            "executed": 987,
            "passed": 926,
            "failed": 0,
            "errors": 0,
            "skipped": 61,
        },
        "judgment": "success",
    }

    lines = _tool()._render_console_evidence_result(snapshot)

    assert lines[:3] == [
        "Build: SUCCESS · modules built 14 of 14 declared on disk (diagnostic) · "
        "class files 56 (diagnostic) · production Java sources 36 (diagnostic)",
        "Tests: EXECUTED · outcomes accounted 987/987 · non-skipped passed 926/926 · "
        "skipped 61 · failed 0 · errors 0 · static declarations 468 (diagnostic)",
        "Coverage: 55.5% line (jacoco-injected)",
    ]
    assert lines[3] == "Official CI: unavailable (no sealed comparison)"
    assert lines[4] == "Verdict (derived): partial"
    assert sum("partial" in line.lower() for line in lines) == 1


def _commons_metric_snapshot():
    """A sealed setup report carrying the two formerly mixed-grain ratios."""
    rates = {
        "build": {
            "modules": {
                "rate": 100.0,
                "band": "fully",
                "numerator": 1,
                "denominator": 1,
            },
            "classes": {
                "band": "unbounded",
                "numerator": 56,
                "denominator": 36,
            },
        },
        "test": {
            "cases": {
                "band": "unbounded",
                "numerator": 987,
                "denominator": 468,
            },
            "modules": {"band": "unavailable", "reason": "not surveyed"},
        },
        "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
    }
    snapshot = _sealed_snapshot(
        verdict="success",
        rates=rates,
        build_system="maven",
    )
    snapshot["canonical_snapshot"]["build_evidence"].update(
        {
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "source_files": 36,
            "compiled_classes": 56,
        }
    )
    snapshot["canonical_snapshot"]["phase_records"] = [
        {
            "phase": "build",
            "termination": "completed",
            "validated_outcome": "success",
        }
    ]
    snapshot["canonical_snapshot"]["test_stats"] = {
        "discovered": 468,
        "unique": {
            "executed": 987,
            "passed": 926,
            "failed": 0,
            "errors": 0,
            "skipped": 61,
        },
        "judgment": "success",
    }
    snapshot["status"].update(
        {
            "tests_total": 987,
            "tests_passed": 926,
            "tests_failed": 0,
            "tests_errors": 0,
            "tests_skipped": 61,
            "tests_flaky": 0,
            "test_judgment": "success",
            "static_test_count": 468,
            # Deliberately retain the historical values: canonical renderers
            # must ignore both mixed-grain summaries.
            "pass_pct": 93.8,
            "execution_rate": 100.0,
        }
    )
    snapshot["physical_evidence"].update(
        {
            "class_files": 56,
            "tests_total": 987,
            "tests_pass_pct": 93.8,
        }
    )
    return snapshot


def test_canonical_report_surfaces_use_same_grain_metrics_only():
    tool = _tool()
    snapshot = _commons_metric_snapshot()

    headline = "\n".join(tool._render_console_evidence_result(snapshot))
    dashboard = "\n".join(tool._render_summary_dashboard(snapshot))
    diagnostics = "\n".join(tool._render_detailed_test_analysis(snapshot))
    issues = "\n".join(tool._render_issues_recommendations(snapshot))
    execution = "\n".join(
        tool._render_execution_details_simplified(
            snapshot,
            {
                "total_runtime": 1,
                "total_iterations": 1,
                "total_thoughts": 1,
                "total_actions": 1,
                "successful_actions": 1,
                "success_rate": 100,
            },
        )
    )
    condensed = tool._generate_condensed_log_output("success", "setup-report.md", {}, snapshot)
    rendered = "\n".join((headline, dashboard, diagnostics, issues, execution, condensed))

    assert "Build: SUCCESS · modules built 1 of 1 declared on disk (diagnostic)" in rendered
    assert "class files 56 (diagnostic)" in rendered
    assert "Tests: EXECUTED · outcomes accounted 987/987" in rendered
    assert "non-skipped passed 926/926" in rendered
    assert "skipped 61 · failed 0 · errors 0" in rendered
    assert "static declarations 468 (diagnostic)" in rendered
    assert "| **Outcomes Accounted** | 987/987 |" in diagnostics
    assert "| **Non-skipped Passed** | 926/926 (100.0%) |" in diagnostics
    assert "| **Skipped** | 61 |" in diagnostics

    assert "56/36" not in rendered
    assert "987/468" not in rendered
    assert "Execution Rate" not in rendered
    assert "**Pass Rate**" not in rendered
    assert "**Test Coverage:**" not in rendered


def test_setup_snapshot_adapter_uses_non_skipped_pass_denominator():
    from sag.agent.verdict_finalizer import RunVerdictSnapshot

    sealed = RunVerdictSnapshot(
        run_id="commons-metrics",
        finalized_at="2026-09-02T12:00:00Z",
        verdict="success",
        build_evidence={
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "compiled_classes": 56,
            "source_files": 36,
        },
        test_stats={
            "discovered": 468,
            "unique": {
                "executed": 987,
                "passed": 926,
                "failed": 0,
                "errors": 0,
                "skipped": 61,
            },
            "raw": {
                "executed": 987,
                "passed": 926,
                "failed": 0,
                "errors": 0,
                "skipped": 61,
            },
            "judgment": "success",
        },
        rates={
            "build": {
                "modules": {
                    "rate": 100.0,
                    "band": "fully",
                    "numerator": 1,
                    "denominator": 1,
                },
                "classes": {"band": "unavailable", "reason": "diagnostic only"},
            },
            "test": {
                "cases": {
                    "rate": 100.0,
                    "band": "fully",
                    "numerator": 987,
                    "denominator": 987,
                },
                "modules": {"band": "unavailable", "reason": "not surveyed"},
            },
            "coverage": {"status": "unavailable", "reason": "coverage pass not run"},
        },
        phase_records=(
            {
                "phase": "build",
                "attempt_id": "build-1",
                "termination": "completed",
                "outcome": "success",
                "validated_outcome": "success",
            },
        ),
    )

    adapted = _tool()._build_report_snapshot(
        sealed,
        report_filename="setup-report.md",
        project_info={"type": "Maven Java Project", "build_system": "Maven"},
    )

    assert adapted["status"]["test_outcomes_accounted"] == 987
    assert adapted["status"]["test_outcomes_expected"] == 987
    assert adapted["status"]["tests_non_skipped"] == 926
    assert adapted["status"]["pass_pct"] == 100.0
    assert adapted["status"]["execution_rate"] is None
    assert adapted["physical_evidence"]["tests_pass_pct"] == 100.0


def _render(tool, snapshot):
    return "\n".join(tool._render_issues_recommendations(snapshot))


def _blocker_count(text):
    match = re.search(r"### Blockers \((\d+)\)", text)
    assert match, f"no Blockers heading rendered:\n{text}"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# A. Blockers derive from sealed evidence
# ---------------------------------------------------------------------------


def test_failed_sealed_verdict_never_reports_zero_blockers():
    """A failed run may not advertise '✅ No blocking issues'."""
    snapshot = _sealed_snapshot(
        verdict="failed",
        build_green=False,
        build_judgment="failed",
        phase_records=[
            {
                "phase": "build",
                "outcome": "failed",
                "validated_outcome": "failed",
                "termination": "blocked",
                "reason": "pip install -e . exited 1: No matching distribution "
                "found for apache-tvm-ffi>=0.1.13",
                "evidence_refs": ["obs:build:9"],
            }
        ],
    )
    text = _render(_tool(), snapshot)

    assert "Blockers (0)" not in text
    assert "No blocking issues" not in text
    assert _blocker_count(text) >= 1
    # verdict outcome + failing phase + its recorded failure signature
    assert "FAILED" in text
    assert "build" in text.lower()
    assert "apache-tvm-ffi" in text


def test_failed_build_evidence_blocks_even_when_verdict_is_partial():
    """Build evidence sealed as failed is a blocker on its own."""
    snapshot = _sealed_snapshot(verdict="partial", build_green=False, build_judgment="failed")
    text = _render(_tool(), snapshot)

    assert "Blockers (0)" not in text
    assert "No blocking issues" not in text
    assert _blocker_count(text) >= 1
    assert "build" in text.lower()


def test_unresolved_conflicts_never_report_zero_blockers():
    """Unresolved (non-adjudicated) conflicts are blocking issues, not silence."""
    snapshot = _sealed_snapshot(verdict="partial", conflicts=["build_receipts_unreadable"])
    text = _render(_tool(), snapshot)

    assert "Blockers (0)" not in text
    assert "No blocking issues" not in text
    assert _blocker_count(text) >= 1
    assert "build_receipts_unreadable" in text


def test_clean_sealed_run_still_reports_no_blockers():
    """The honest clean path is unchanged — no invented blockers."""
    snapshot = _sealed_snapshot(verdict="success")
    text = _render(_tool(), snapshot)

    assert "### Blockers (0)" in text
    assert "✅ No blocking issues" in text


def test_derived_blockers_do_not_duplicate_attention_blockers():
    """The legacy attention rules already flag build failure; derive once."""
    snapshot = _sealed_snapshot(
        verdict="failed",
        build_green=False,
        build_judgment="failed",
        attention_raw=[
            {
                "severity": "BLOCKER",
                "icon": "🔴",
                "message": "Build failed - compilation or packaging incomplete.",
            }
        ],
    )
    text = _render(_tool(), snapshot)

    assert text.count("Build failed - compilation or packaging incomplete.") == 1
    assert "Blockers (0)" not in text


# ---------------------------------------------------------------------------
# B. Recommendations derive from surveyed facts
# ---------------------------------------------------------------------------


def test_recommendations_quote_surveyed_install_and_smoke_facts():
    """Surveyed coordinates replace the generic pip/pytest prose."""
    # The strict live reader serves only complete v1 manifests, whose installer
    # grammar pins the venv placeholder form — the surveyed install/smoke facts
    # under test ride the smallest complete Python manifest and are still
    # quoted VERBATIM, never rewritten into generic ecosystem prose.
    orch = FakeOrch(
        manifest=complete_python_build_requirements_v1(
            project_root="/workspace/tvm",
            test_root="/workspace/tvm/tests/python",
            python_install_commands=[
                "{venv}/bin/python -m pip install -e .",
                "{venv}/bin/python -m pip install ml_dtypes numpy",
            ],
            python_smoke_candidates=[
                {"path": "tests/python/test_runtime.py", "source": "filesystem:test-file"}
            ],
        )
    )
    snapshot = _sealed_snapshot(verdict="partial")
    text = _render(_tool(docker_orchestrator=orch), snapshot)

    assert "pip install -e . && pytest" not in text
    assert "{venv}/bin/python -m pip install -e ." in text
    assert "{venv}/bin/python -m pip install ml_dtypes numpy" in text
    assert "tests/python/test_runtime.py" in text


def test_recommendations_quote_surveyed_build_coordinates():
    """A Java run quotes the surveyed coordinates, not a canned mvn line."""
    context_manager = FakeContextManager(
        environment_summary={
            "build_recommendation": {
                "build_system": "maven",
                "build_root": "/workspace/bigtop",
                "test_root": "/workspace/bigtop/bigtop-tests",
                "test_system": "gradle",
            }
        }
    )
    snapshot = _sealed_snapshot(verdict="partial", build_system="maven")
    text = _render(_tool(context_manager=context_manager), snapshot)

    assert "mvn clean test -DskipTests=false" not in text
    assert "/workspace/bigtop" in text
    assert "/workspace/bigtop/bigtop-tests" in text
    assert "gradle" in text


def test_recommendations_without_survey_facts_name_the_evidence_gap():
    """A reachable-but-empty survey is reported as a gap, never as advice."""
    snapshot = _sealed_snapshot(verdict="failed", build_green=False, build_judgment="failed")
    text = _render(_tool(docker_orchestrator=FakeOrch()), snapshot)

    assert "pip install -e . && pytest" not in text
    assert "mvn clean test" not in text
    assert "no surveyed" in text.lower()
    assert "project(action='analyze')" not in text
    assert "no reproduction or repair command can be inferred" in text


def test_execution_details_render_only_the_recorded_command():
    tool = _tool()
    lines = tool._render_execution_details_simplified(
        {
            "last_command": {
                "tool": "python",
                "command": "python -m pytest tests/unit/test_one.py",
                "workdir": "/workspace/project",
            }
        },
        None,
    )
    text = "\n".join(lines)

    assert "Last Recorded Tool" in text
    assert "python -m pytest tests/unit/test_one.py" in text
    assert "/workspace/project" in text
    assert "mvn clean install" not in text
    assert "mvn test" not in text
    assert "-DskipTests" not in text


# ---------------------------------------------------------------------------
# C. Collection failures are the root cause, never executed tests (Plan 4 T3)
# ---------------------------------------------------------------------------

# The first line of the dominant collection error in the live TVM artifact
# (`pytest-attempt-000001.xml`, session_20260726_132903_18116) — the fact the
# audit found nowhere in the report that was describing it.
TVM_COLLECTION_ROOT_CAUSE = (
    "RuntimeError: None of the following targets are supported by this build "
    "of TVM: ['llvm', 'llvm -keys=cpu -num-cores=8']"
)


def _collection_failure_snapshot(
    *,
    collection_errors=28,
    collection_errors_skipped=28,
    summary=TVM_COLLECTION_ROOT_CAUSE,
):
    """A sealed TVM-shaped snapshot: every pytest node was a collection node.

    Post-Plan-4-Task-2 seal shape: the 28 collection errors and their 28
    paired empty-classname skips are OUT of executed/errors/skipped and in
    ``collection_errors`` / ``collection_errors_skipped`` on the sealed
    ``test_stats``, which carries the dominant error's first line.
    """
    snapshot = _sealed_snapshot(
        verdict="failed",
        # TVM's build DID run (libtvm_ffi.so exists) — the wall is a
        # toolchain capability, so build evidence stays green here and the
        # collection failure must stand on its own as the root cause.
        build_green=True,
        build_judgment="success",
        phase_records=[
            {
                "phase": "test",
                "outcome": "failed",
                "validated_outcome": "failed",
                "termination": "blocked",
                "reason": "pytest exited 2",
                "evidence_refs": ["obs:test:4"],
            }
        ],
    )
    snapshot["status"].update(
        {
            "tests_total": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "tests_errors": 0,
            "tests_skipped": 0,
            "pass_pct": None,
            "static_test_count": None,
        }
    )
    zero = {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    snapshot["canonical_snapshot"]["test_stats"] = {
        "discovered": None,
        "unique": dict(zero),
        "raw": dict(zero),
        "flaky_count": 0,
        "judgment": "failed",
        "collection_errors": collection_errors,
        "collection_errors_skipped": collection_errors_skipped,
        "collection_error_summary": summary,
    }
    return snapshot


def _render_tests(tool, snapshot):
    return "\n".join(tool._render_detailed_test_analysis(snapshot))


def test_collection_failure_section_states_zero_executed_and_quotes_root_cause():
    """The test section reports the collection failure and its structured cause."""
    text = _render_tests(_tool(), _collection_failure_snapshot())

    assert "Test collection failed for 28 files — 0 tests executed" in text
    # Verbatim — the report quotes the structured error, it does not paraphrase.
    assert TVM_COLLECTION_ROOT_CAUSE in text


def test_collection_nodes_are_never_rendered_as_executed_tests():
    """28 errors + 28 skips are collection artifacts, not a 56-test run."""
    text = _render_tests(_tool(), _collection_failure_snapshot())

    assert "56" not in text
    assert "28 tests executed" not in text
    # Nothing executed: the executed-test metrics tables must not render at all.
    assert "Unique Tests Executed" not in text
    assert "Test Execution Breakdown" not in text
    # The counts stay visible, explicitly labelled as collection artifacts.
    assert "28 collection errors" in text
    assert "28 collection-artifact skips" in text


def test_collection_failure_derives_a_blocker_quoting_the_structured_error():
    """The blockers section derives its blocker from the collection evidence."""
    text = _render(_tool(), _collection_failure_snapshot())

    assert "Blockers (0)" not in text
    assert "No blocking issues" not in text
    assert _blocker_count(text) >= 1
    matching = [line for line in text.splitlines() if "Test collection failed for 28 files" in line]
    assert len(matching) == 1, text
    assert "0 tests executed" in matching[0]
    assert TVM_COLLECTION_ROOT_CAUSE in matching[0]


def test_report_names_only_the_sealed_latest_test_attempt_scope_and_command():
    """A raw collect sidecar cannot replace the attempt the seal selected."""
    from sag.tools.internal.python_tool import COLLECTED_JSON

    orch = FakeOrch()
    orch.files[COLLECTED_JSON] = json.dumps(
        {"collected": 1, "scope": "forged-container-sidecar", "selected": 1}
    )
    snapshot = _collection_failure_snapshot()
    snapshot["latest_test_attempt"] = {
        "collected": 11702,
        "scope": "full",
        "selected": 11702,
    }
    snapshot["last_command"] = {
        "command": "/workspace/.venv/bin/python -m pytest --junitxml=/workspace/"
        ".setup_agent/pytest-reports/pytest-attempt-000001.xml",
        "tool": "python",
        "workdir": "/workspace/tvm",
    }

    text = _render_tests(_tool(docker_orchestrator=orch), snapshot)

    assert "scope=full" in text
    assert "collected=11702" in text
    assert "forged-container-sidecar" not in text
    assert "/workspace/.venv/bin/python -m pytest" in text
    assert not any(COLLECTED_JSON in command for command in orch.commands)


def test_collection_facts_are_read_from_the_evidence_result_projection():
    """The same facts are honoured wherever the seal projects them."""
    snapshot = _sealed_snapshot(verdict="failed")
    snapshot["status"]["tests_total"] = 0
    snapshot["evidence_result"]["test_stats"] = {
        "discovered": None,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "collection_errors": 3,
        "collection_error_summary": "ImportError: libtvm_ffi.so: cannot open shared object file",
    }

    text = _render_tests(_tool(), snapshot)

    assert "Test collection failed for 3 files — 0 tests executed" in text
    assert "ImportError: libtvm_ffi.so: cannot open shared object file" in text


def test_run_without_collection_errors_renders_outcome_accounting():
    """A clean sealed run uses same-grain outcome accounting."""
    snapshot = _sealed_snapshot(verdict="success")
    snapshot["status"].update(
        {
            "tests_total": 982,
            "tests_passed": 921,
            "tests_failed": 0,
            "tests_errors": 0,
            "tests_skipped": 61,
            "pass_pct": 93.8,
        }
    )

    text = _render_tests(_tool(), snapshot)

    assert "Test collection failed" not in text
    assert "Latest test attempt" not in text
    assert "| **Outcomes Accounted** | 982/982 |" in text
    assert "| **Non-skipped Passed** | 921/921 (100.0%) |" in text


def test_no_tests_and_no_collection_facts_renders_no_test_section():
    """No observations still renders the explicit metrics-v2 availability layer."""
    lines = _tool()._render_detailed_test_analysis(_sealed_snapshot(verdict="failed"))
    assert lines[0] == "## 🧾 Metrics-v2 Evidence Layers"
    assert any("Claimed latest subjects: unavailable" in line for line in lines)
    assert "## 🧪 Snapshot Test Diagnostics" not in lines
