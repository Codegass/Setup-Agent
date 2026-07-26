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

B. The recommendations section printed generic ecosystem prose
   (``pip install -e . && pytest`` / ``mvn clean test -DskipTests=false``)
   that contradicted the coordinates the survey actually recorded. When
   surveyed facts exist the report must quote them; when a survey source is
   reachable but recorded nothing, the report must name that evidence gap
   instead of inventing commands.
"""

import json
import re

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.report_tool import ReportTool

# ---------------------------------------------------------------------------
# House fixtures
# ---------------------------------------------------------------------------


class FakeOrch:
    """In-memory container FS (house pattern from tests/test_build_preflight.py)."""

    def __init__(self, manifest=None):
        self.files = {}
        if manifest is not None:
            self.files[REQUIREMENTS_PATH] = json.dumps(manifest)

    def execute_command(self, cmd, workdir=None):
        if cmd.startswith("cat "):
            path = cmd.split("cat ", 1)[1].strip()
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {"success": False, "exit_code": 1, "output": "No such file"}
        return {"success": True, "exit_code": 0, "output": ""}


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
    }
    raw = attention_raw
    if raw is None:
        # The adapter's actual behaviour: every conflict lands as INFO.
        raw = [{"severity": "INFO", "icon": "INFO", "message": conflict} for conflict in conflicts]
    return {
        "mode": "setup",
        "status": {"verdict": verdict, "overall": verdict},
        "project": {"type": "Python", "build_system": build_system},
        "phases": {"clone": True, "build": build_green, "test": None},
        "physical_evidence": {"build_system": build_system},
        "test_history": {},
        "per_module": {},
        "flags": {},
        "evidence_result": {"status": verdict, "conflicts": list(conflicts)},
        "attention": {"items": [], "raw": raw, "ignored_lines": 0},
        "canonical_snapshot": canonical,
    }


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
    snapshot = _sealed_snapshot(verdict="partial", conflicts=["build_modules_incomplete"])
    text = _render(_tool(), snapshot)

    assert "Blockers (0)" not in text
    assert "No blocking issues" not in text
    assert _blocker_count(text) >= 1
    assert "build_modules_incomplete" in text


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
    orch = FakeOrch(
        manifest={
            "build_root": "/workspace/tvm",
            "test_root": "/workspace/tvm/tests/python",
            "python_install_commands": [
                "/workspace/.venv/bin/python -m pip install -e . --no-deps",
                "/workspace/.venv/bin/python -m pip install ml_dtypes numpy",
            ],
            "python_smoke_candidates": [
                {"path": "tests/python/test_runtime.py", "source": "pytest_ini"}
            ],
        }
    )
    snapshot = _sealed_snapshot(verdict="partial")
    text = _render(_tool(docker_orchestrator=orch), snapshot)

    assert "pip install -e . && pytest" not in text
    assert "/workspace/.venv/bin/python -m pip install -e . --no-deps" in text
    assert "/workspace/.venv/bin/python -m pip install ml_dtypes numpy" in text
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
    assert "project(action='analyze')" in text
