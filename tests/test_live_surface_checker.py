import json

from scripts.collect_control_layer_ab import check_surfaces, prepare_surface_artifacts


def _artifacts(tmp_path, *, cli_passed=541, cli_flaky=3):
    setup = tmp_path / ".setup_agent"
    setup.mkdir()
    (setup / "verdict.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "run_id": "surface-run",
                "finalized_at": "2026-07-17T12:00:00Z",
                "verdict": "partial",
                "build_evidence": {
                    "observed": True,
                    "green": False,
                    "outcome": "partial",
                    "evidence_status": "verified",
                    "refs": [],
                },
                "test_stats": {
                    "discovered": 541,
                    "unique": {
                        "executed": 541,
                        "passed": 541,
                        "failed": 0,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "raw": {
                        "executed": 544,
                        "passed": 541,
                        "failed": 3,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "flaky_count": 3,
                    "judgment": "success",
                },
                "conflicts": [],
                "phase_records": [],
                "input_refs": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "report.md").write_text(
        "# PARTIAL\nTests: 541 / 541 passed (3 flaky)\n", encoding="utf-8"
    )
    (tmp_path / "condensed.txt").write_text(
        "PARTIAL | Tests 541/541 passed (3 flaky)\n", encoding="utf-8"
    )
    (tmp_path / "cli-output.log").write_text(
        f"PARTIAL\nTests: {cli_passed} / 541 passed ({cli_flaky} flaky)\n",
        encoding="utf-8",
    )
    (tmp_path / "web-read-model.json").write_text(
        json.dumps(
            {
                "verdict": "partial",
                "test": {"total": 541, "passed": 541, "flakyCount": 3},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "surface-artifacts.json").write_text(
        json.dumps(
            {
                "markdown": "report.md",
                "condensed": "condensed.txt",
                "cli": "cli-output.log",
                "web": "web-read-model.json",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_surface_checker_compares_rendered_values_to_snapshot(tmp_path):
    result = check_surfaces(_artifacts(tmp_path))

    assert result.ok is True
    assert result.mismatches == ()


def test_surface_checker_fails_on_cli_drift(tmp_path):
    result = check_surfaces(_artifacts(tmp_path, cli_passed=540, cli_flaky=0))

    assert result.ok is False
    assert result.mismatches[0].surface == "cli"
    assert {item.field for item in result.mismatches} >= {"passed", "flaky_count"}


def test_surface_checker_never_accepts_raw_retry_total_as_primary(tmp_path):
    artifacts = _artifacts(tmp_path)
    (artifacts / "condensed.txt").write_text(
        "PARTIAL | Tests 544/544 passed (3 flaky)\n", encoding="utf-8"
    )

    result = check_surfaces(artifacts)

    assert result.ok is False
    assert any(item.surface == "condensed" and item.field == "total" for item in result.mismatches)


def test_surface_checker_checks_rendered_failure_skip_and_raw_counts(tmp_path):
    artifacts = _artifacts(tmp_path)
    (artifacts / "cli-output.log").write_text(
        "Verdict: PARTIAL\n"
        "Tests: 541 unique (541 passed (3 flaky), 1 failed, 0 errors, 2 skipped)\n"
        "Raw executions (diagnostic): 999\n",
        encoding="utf-8",
    )

    result = check_surfaces(artifacts)

    assert result.ok is False
    assert {item.field for item in result.mismatches if item.surface == "cli"} >= {
        "failed",
        "skipped",
        "raw_executions",
    }


def test_surface_checker_parses_the_production_cli_shape(tmp_path):
    artifacts = _artifacts(tmp_path)
    (artifacts / "cli-output.log").write_text(
        "Project: paramiko\n"
        "Verdict: PARTIAL\n"
        "Tests: 541 unique (541 passed (3 flaky), 0 failed, 0 errors, 0 skipped)\n"
        "Raw executions (diagnostic): 544\n",
        encoding="utf-8",
    )

    result = check_surfaces(artifacts)

    assert result.ok is True


def test_prepare_surface_artifacts_uses_recorded_outputs_and_web_projection(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    session = _artifacts(session)
    (session / "surface-artifacts.json").unlink()
    (session / "report.md").rename(session / "setup-report-20260717-120000.md")
    (session / "condensed.txt").unlink()
    (session / "cli-output.log").unlink()
    (session / "web-read-model.json").unlink()
    contexts = session / ".setup_agent" / "contexts"
    contexts.mkdir()
    (contexts / "phase_report.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "type": "action",
                        "tool_name": "report",
                        "output": (
                            "🎯 SETUP COMPLETED: ⚠️ PARTIAL\n"
                            "🧪 Tests: 541 executed (pass rate 100.0%, 3 flaky)\n"
                            "Result: PARTIAL\n"
                            "Tests: 541 / 541 passed (3 flaky), 100.0% pass rate, "
                            "0 failed, 0 skipped"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    captured_cli = tmp_path / "captured-cli.log"
    captured_cli.write_text(
        "Project: paramiko\n"
        "Verdict: PARTIAL\n"
        "Tests: 541 unique (541 passed (3 flaky), 0 failed, 0 errors, 0 skipped)\n"
        "Raw executions (diagnostic): 544\n",
        encoding="utf-8",
    )

    manifest_path = prepare_surface_artifacts(session, captured_cli)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "markdown": "setup-report-20260717-120000.md",
        "condensed": "surface-condensed.log",
        "cli": "surface-cli.log",
        "web": "web-read-model.json",
    }
    assert "SETUP COMPLETED" in (session / manifest["condensed"]).read_text(encoding="utf-8")
    web = json.loads((session / manifest["web"]).read_text(encoding="utf-8"))
    assert web["test"]["raw_executions"] == 544
    assert check_surfaces(session).ok is True
