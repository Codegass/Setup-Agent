"""Regression over five sealed campaign cells (2026-10-21 fifty-project run).

Each directory under ``fixtures/verdict_cells`` holds the finalizer inputs of
one sealed cell, trimmed to what the measured dimensions read; ``sources.json``
names each file's sealed source and both digests. The facts are re-read the way
the finalizer reads them live, re-sealed as a current record, and the word the
reader accepts for them is asserted.
"""

import hashlib
import json
from pathlib import Path

import pytest

from sag.agent.acceptance_task import AcceptanceTask, TaskCompletionSnapshot
from sag.agent.ci_comparison import PinnedCITarget
from sag.agent.verdict_dimensions import ci_command, official_ci, run_control, sag_checks
from sag.agent.verdict_finalizer import VERDICT_SCHEMA_VERSION, validate_verdict_snapshot_v3
from sag.metrics.target_record import TargetRecord

CELLS = Path(__file__).parent / "fixtures" / "verdict_cells"


def _json(path: Path):
    return json.loads(path.read_text())


def _cell(name: str) -> dict:
    root = CELLS / name
    verdict = _json(root / "verdict.json")
    task = AcceptanceTask.model_validate(_json(root / "task.json"))
    raw_target = (root / "ci-target.json").read_bytes()
    target = PinnedCITarget(
        record=TargetRecord.model_validate_json(raw_target),
        raw_sha256=hashlib.sha256(raw_target).hexdigest(),
    )
    completion = TaskCompletionSnapshot.model_validate(verdict["task_completion"])
    receipts = {
        path.stem: _json(path) for path in sorted((root / "invocation_receipts").glob("*.json"))
    }
    obligations = {
        path.stem: _json(path) for path in sorted((root / "job_obligations").glob("*.json"))
    }
    events = [json.loads(line) for line in (root / "control_events.jsonl").read_text().splitlines()]
    live_reasons = {
        event["payload"]["job_id"]: event["payload"].get("close_reason")
        for event in events
        if event["kind"] == "job_live_at_close"
    }
    closes = [event["payload"]["reason"] for event in events if event["kind"] == "evidence_close"]
    requirements = _json(root / "build_requirements.json")["java_requirements"] or {}
    tests = [record for record in verdict["phase_records"] if record["phase"] == "test"]
    conflicts = tuple(verdict["conflicts"])
    project_root = f"/workspace/{task.repo.split('/')[1]}"
    dimensions = {
        "ci_command": ci_command(completion, task=task, receipts=receipts),
        "official_ci": official_ci(target, completion=completion, receipts=receipts),
        "sag_checks": sag_checks(
            conflicts=conflicts,
            test_judgment=verdict["test_stats"]["judgment"],
            test_reason=(tests[-1]["reason"] or None) if tests else None,
            java_unresolved=tuple(requirements.get("unresolved") or ()),
        ),
        "run_control": run_control(
            conflicts=conflicts,
            close_reason=closes[-1] if closes else None,
            phase_records=verdict["phase_records"],
            live_job_reasons=live_reasons,
            obligations=obligations,
            task=task,
            project_root=project_root,
        ),
    }
    return {"verdict": verdict, **dimensions}


def _resealed_word(cell: dict) -> str:
    """The word a current reader accepts for these facts, found by trying each."""

    payload = {
        **cell["verdict"],
        "schema_version": VERDICT_SCHEMA_VERSION,
        "ci_command": cell["ci_command"].model_dump(mode="json"),
        "official_ci": cell["official_ci"].model_dump(mode="json"),
        "sag_checks": [check.model_dump(mode="json") for check in cell["sag_checks"]],
        "run_control": cell["run_control"].model_dump(mode="json"),
    }
    accepted = []
    for word in ("success", "partial", "failed", "unknown"):
        try:
            validate_verdict_snapshot_v3({**payload, "verdict": word})
        except ValueError:
            continue
        accepted.append(word)
    assert len(accepted) == 1, accepted
    return accepted[0]


@pytest.mark.parametrize("name", sorted(path.name for path in CELLS.iterdir()))
def test_fixture_files_are_the_copies_their_sources_json_names(name):
    sources = _json(CELLS / name / "sources.json")
    listed = {entry["fixture"] for entry in sources["files"]}
    on_disk = {
        str(path.relative_to(CELLS / name))
        for path in (CELLS / name).rglob("*")
        if path.is_file() and path.name != "sources.json"
    }
    assert listed == on_disk
    for entry in sources["files"]:
        data = (CELLS / name / entry["fixture"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["fixture_sha256"]
        assert entry["source"].startswith(f"output/benchmark-fifty-20261021/runs/{name}/")
        assert len(entry["source_sha256"]) == 64


def test_casbin_passed_with_sources_rewritten_by_its_own_tests():
    cell = _cell("casbin-jcasbin-r3")
    assert cell["verdict"]["verdict"] == "success"
    command = cell["ci_command"]
    assert (command.result, command.steps_passed, command.steps_total) == ("passed", 1, 1)
    assert (command.toolchain_as_pinned, command.pinned_java) == ("yes", (8,))
    assert command.source_unchanged == "yes"
    assert command.source_detail == (
        "examples/basic_policy.csv, examples/rbac_policy.csv, examples/rbac_with_domains_policy.csv"
    )
    ci = cell["official_ci"]
    assert (ci.cell, ci.ci_modules, ci.agent_modules_built) == ("build", (".",), (".",))
    assert (ci.agent_tests_executed, ci.ci_tests_executed) == (266, 532)
    assert all(check.confirmed for check in cell["sag_checks"])
    assert cell["run_control"].ended == "normally"
    assert _resealed_word(cell) == "success"


def test_httpcomponents_client_failed_with_four_errors():
    cell = _cell("httpcomponents-client")
    assert cell["verdict"]["verdict"] == "partial"
    command = cell["ci_command"]
    assert (command.result, command.steps_passed, command.steps_total) == ("failed", 0, 1)
    assert command.first_unpassed_step.model_dump() == {
        "id": "ci-step-1",
        "status": "failed",
        "exit_code": 1,
    }
    assert command.toolchain_as_pinned == "yes"
    assert (command.pinned_java, command.pinned_maven) == ((17,), ("3.9.16",))
    ci = cell["official_ci"]
    assert ci.cell == "Apache Jenkins httpcomponents-client Linux JDK 17 #225"
    assert len(ci.ci_modules) == 9
    assert len(set(ci.agent_modules_built) & set(ci.ci_modules)) == 7
    assert (ci.agent_tests_executed, ci.ci_tests_executed) == (2679, 2762)
    assert (ci.agent_failures, ci.agent_errors) == (0, 4)
    assert [check.confirmed for check in cell["sag_checks"]] == [True, False, False, True]
    assert cell["run_control"].ended == "normally"
    assert _resealed_word(cell) == "failed"


def test_mug_passed_with_the_java_requirement_undecided():
    cell = _cell("mug")
    assert cell["verdict"]["verdict"] == "partial"
    command = cell["ci_command"]
    assert (command.result, command.toolchain_as_pinned) == ("passed", "yes")
    assert (command.pinned_java, command.pinned_maven) == ((24,), ("3.9.16",))
    ci = cell["official_ci"]
    assert (ci.agent_tests_executed, ci.ci_tests_executed) == (55861, 55861)
    assert ci.ci_modules is None and len(ci.agent_modules_built) == 16
    checks = cell["sag_checks"]
    assert [check.confirmed for check in checks] == [False, True, True, True]
    assert checks[0].reason == "the 'playground' profile's activation could not be evaluated"
    assert _resealed_word(cell) == "success"


def test_helix_did_not_finish_at_the_time_limit():
    cell = _cell("helix")
    assert cell["verdict"]["verdict"] == "partial"
    command = cell["ci_command"]
    assert (command.result, command.steps_passed, command.steps_total) == ("not_finished", 1, 2)
    assert command.first_unpassed_step.model_dump() == {
        "id": "ci-step-2",
        "status": "missing",
        "exit_code": None,
    }
    assert command.toolchain_as_pinned == "unavailable"
    assert command.toolchain_reason == (
        "step ci-step-2 did not finish, so its Java version was not recorded"
    )
    ci = cell["official_ci"]
    # The CI record states no test count for this cell, so none is compared.
    assert ci.ci_tests_executed is None and ci.ci_modules is None
    control = cell["run_control"]
    assert (control.ended, control.commands_still_running) == ("time_limit", 1)
    assert control.detail == "2 background commands were still running when the run stopped"
    assert _resealed_word(cell) == "partial"


def test_commons_vfs_stopped_after_a_job_could_not_be_stopped_cleanly():
    cell = _cell("commons-vfs-r2")
    assert cell["verdict"]["verdict"] == "failed"
    command = cell["ci_command"]
    assert (command.result, command.steps_passed, command.steps_total) == ("not_finished", 0, 1)
    assert command.first_unpassed_step.id == "ci-step-1"
    ci = cell["official_ci"]
    assert ci.ci_tests_executed == 2758
    assert ci.agent_tests_executed is None and ci.agent_modules_built is None
    control = cell["run_control"]
    assert (control.ended, control.commands_still_running) == ("job_control_failure", 1)
    assert _resealed_word(cell) == "partial"
