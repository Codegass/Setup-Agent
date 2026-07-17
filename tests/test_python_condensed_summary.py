# tests/test_python_condensed_summary.py
"""Python projects render python build evidence in the condensed summary.

Live-run Java-ism: on python projects the condensed summary printed
"🧾 Build artifacts: 0 .class, 0 .jar" — the Java artifact counters are
meaningless there (python has no .class/JAR analog; the evidence ladder
venv -> pip check -> imports -> compileall IS the build evidence, see
PhysicalValidator._verify_python_build). Fix under test: when the build
system is python, render the ladder's fingerprint_details instead, e.g.
"🧾 Build evidence: venv ✓, pip check ✓, imports ✓, compileall 100%".
Java/Maven/Gradle snapshots keep the artifacts line byte-for-byte
(tests/test_reporting_condensed_summary.py stays green untouched).
"""

from sag.reporting.utils import render_condensed_summary
from sag.tools.report_tool import ReportTool


def _python_snapshot(fingerprint_details, class_files=0, jar_files=0):
    return {
        "status": {"verdict": "success"},
        "project": {"type": "Python Project", "build_system": "pip/poetry"},
        "phases": {"clone": True, "build": True, "test": True},
        "physical_evidence": {
            "class_files": class_files,
            "jar_files": jar_files,
            "tests_total": 1287,
            "tests_pass_pct": 100.0,
            "build_system": "python",
            "fingerprint_details": fingerprint_details,
        },
        "attention": {"items": []},
        "report_path": "/workspace/setup-report.md",
    }


def test_python_project_renders_ladder_evidence_not_java_artifacts():
    """LIVE-RUN REPRODUCTION: python run rendered '0 .class, 0 .jar'."""
    out = render_condensed_summary(
        _python_snapshot(
            {
                "venv_exists": True,
                "pip_check_clean": True,
                "imports_ok": True,
                "import_failures": [],
                "compileall_coverage": 1.0,
                "ext_modules_ok": None,  # no declared C-extensions: rung unknown
            }
        )
    )

    assert "🧾 Build evidence: venv ✓, pip check ✓, imports ✓, compileall 100%" in out
    assert ".class" not in out
    assert ".jar" not in out
    # Unknown rungs are never invented evidence.
    assert "C-extensions" not in out


def test_python_failed_rungs_render_crosses():
    out = render_condensed_summary(
        _python_snapshot(
            {
                "venv_exists": True,
                "pip_check_clean": False,
                "imports_ok": False,
                "import_failures": ["yaml"],
                "compileall_coverage": 0.62,
                "ext_modules_ok": False,
            }
        )
    )

    assert "venv ✓" in out
    assert "pip check ✗" in out
    assert "imports ✗" in out
    assert "compileall 62%" in out
    assert "C-extensions ✗" in out


def test_java_project_keeps_artifact_line():
    out = render_condensed_summary(
        {
            "status": {"verdict": "success"},
            "project": {"type": "Maven Java Project", "build_system": "Maven"},
            "phases": {"clone": True, "build": True, "test": True},
            "physical_evidence": {
                "class_files": 10,
                "jar_files": 1,
                "tests_total": 5,
                "tests_pass_pct": 100.0,
            },
            "attention": {"items": []},
            "report_path": "/workspace/setup-report.md",
        }
    )

    assert "🧾 Build artifacts: 10 .class, 1 .jar" in out
    assert "Build evidence:" not in out


class _PythonDetectingValidator:
    """Physical detection says python; no Java module scan may run."""

    def _detect_build_system(self, project_dir):
        return "python"


def test_snapshot_builder_sources_fingerprint_details_from_validator():
    """_build_report_snapshot must carry the validator's evidence ladder
    (build_status.evidence.fingerprint_details) into physical_evidence so the
    renderer has something honest to print on python projects."""
    tool = ReportTool()
    tool.physical_validator = _PythonDetectingValidator()

    ladder = {
        "venv_exists": True,
        "pip_check_clean": True,
        "imports_ok": True,
        "import_failures": [],
        "compileall_coverage": 1.0,
        "ext_modules_ok": None,
    }
    snapshot = tool._build_legacy_report_snapshot(
        "success",
        "setup-report.md",
        {"type": "Python Project", "build_system": "pip/poetry", "directory": "/workspace/pyyaml"},
        {
            "physical_validation": {
                "build_status": {
                    "success": True,
                    "build_complete": True,
                    "evidence": {
                        "build_system": "python",
                        "fingerprint_details": ladder,
                    },
                },
                "test_analysis": {},
                "class_files": 0,
                "jar_files": 0,
            }
        },
        {"test_history": {"aggregate": {}}},
    )

    evidence = snapshot["physical_evidence"]
    assert evidence["build_system"] == "python"
    assert evidence["fingerprint_details"] == ladder

    out = render_condensed_summary(snapshot)
    assert "🧾 Build evidence: venv ✓, pip check ✓, imports ✓, compileall 100%" in out
    assert "0 .class" not in out
