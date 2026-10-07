"""The SAG adapter exports authorized bytes without turning missing facts green."""

from types import SimpleNamespace

import pytest
from container_evidence_fakes import add_published_mutable_json, complete_run_pin
from test_acceptance_task import completion, dispatch, retain, task_run
from test_ci_comparison import ROOT, SHA

from sag.agent.evidence_publications import RUN_PIN_LOGICAL_ARTIFACT_ID
from sag.agent.verdict_finalizer import VerdictFinalizer
from sag.benchmark.evaluator import evaluate
from sag.benchmark.requirements import bound_file, evaluation_identity, load_json
from sag.benchmark.sag_adapter import export_sag_requirements, _command_matches


@pytest.mark.parametrize('receipt_argv,cwd,executable,expected', [
    ('/workspace/p/mvnw -B clean install', '/workspace/p', '/workspace/p/mvnw', True),
    ('/elsewhere/mvnw -B clean install', '/workspace/p', '/elsewhere/mvnw', False),
    ('/workspace/p/mvnw -B clean install', '/workspace/other', '/workspace/p/mvnw', False),
    ('/workspace/p/mvnw -B clean install', '/workspace/p', None, False),
    ('/workspace/p/mvnw -B clean install', '/workspace/p', '/opt/bin/mvn', False),
    ('/workspace/p/mvnw -B clean package', '/workspace/p', '/workspace/p/mvnw', False),
    ('/opt/bin/mvn -B clean install', '/workspace/p', '/opt/bin/mvn', False),
])
def test_relative_wrapper_matches_only_its_recorded_absolute_executable(receipt_argv, cwd, executable, expected):
    step = SimpleNamespace(cwd='.', argv=('./mvnw', '-B', 'clean', 'install'))
    receipt = {'actual_cwd': cwd, 'argv': receipt_argv, 'toolchain_fingerprint': {'executable': executable}}
    assert _command_matches(step, receipt, '/workspace/p') is expected


def test_relative_wrapper_resolves_from_the_frozen_step_directory():
    step = SimpleNamespace(cwd='subproject', argv=('../mvnw', 'test'))
    receipt = {'actual_cwd': '/workspace/p/subproject', 'argv': '/workspace/p/mvnw test',
               'toolchain_fingerprint': {'executable': '/workspace/p/mvnw'}}
    assert _command_matches(step, receipt, '/workspace/p') is True


def prepare(tmp_path):
    run = task_run(tmp_path, "mvn clean install")
    row = {"id": "compile", "step_id": "step-0", "kind": "compile", "module": "p",
           "scope": {"status": "resolved", "module_path": "."},
           "validation": {"rule": "native_goal", "goals": ["compiler:compile"]}}
    spec = {"schema_version": 2, "policy_version": "ci-requirements-v2", "project_id": "demo",
            "task_sha256": run.task.sha256, "annotation_completeness": {"status": "complete"},
            "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
            "requirements": [row]}
    protocol = evaluation_identity(spec) | {"requirements_file_sha256": "d" * 64}
    pin = complete_run_pin(run.state.run_id, SHA)
    pin["sanitized_config"].update(
        acceptance_task={"sha256": run.task.sha256, "definition": run.task.model_dump(mode="json")},
        evaluation_protocol=protocol,
    )
    add_published_mutable_json(run.fs, run.fs, path="/workspace/.setup_agent/run-pin.json",
                               record_kind="run_pin", record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
                               logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID, payload=pin)
    retained = retain(run, dispatch(run, "mvn clean install"))
    closed = completion(run)
    assert closed.status == "complete"
    base = tmp_path / "session"

    def export(**changes):
        args = dict(validator=run.validator, project_root=ROOT, task=run.task,
                    completion=closed, spec=spec, protocol=protocol,
                    output_storage=run.storage, session_dir=base)
        args.update(changes)
        paths = export_sag_requirements(run.fs, run.state, **args)
        record = load_json(bound_file(base, paths["run"]))
        score = load_json(bound_file(base, paths["result"]))
        return paths, record, score
    return run, spec, protocol, retained, closed, base, export


def test_live_receipts_and_full_output_become_shared_records(tmp_path):
    run, spec, _protocol, _retained, _closed, base, export = prepare(tmp_path)
    paths, record, score = export()
    assert len(record["invocations"]) == 1
    invocation = load_json(bound_file(base, record["invocations"][0]))
    source = load_json(bound_file(base, invocation["source_receipt"]))
    assert invocation["invocation_id"] == source["contract_id"]
    assert invocation["receipt_id"] == source["receipt_id"]
    assert invocation["runtime"]["sag_dispatch_receipt"] == invocation["source_receipt"]
    assert bound_file(base, invocation["log"]).read_text() == "BUILD SUCCESS"
    assert invocation["log_complete"] is True
    assert invocation["reports_collection_complete"] is False
    assert invocation["artifacts"] == []
    assert score["commands"][0]["status"] == "passed"
    assert score["status"] == "unavailable"  # No invented configuration/worktree proof.
    assert score["human_interventions"] is None
    shared = evaluate(run.task.model_dump(mode="json"), spec, record, base)
    assert score["requirements"] == shared["requirements"]
    assert score["status"] == shared["status"] == paths["status"]


def test_changed_parsed_requirements_cannot_borrow_the_old_run_pin(tmp_path):
    _run, spec, _protocol, _retained, _closed, _base, export = prepare(tmp_path)
    spec["requirements"][0]["validation"]["goals"] = ["compiler:testCompile"]
    _paths, record, score = export()
    assert record["evaluation_identity"] is None
    assert record["invocations"] == []
    assert score["status"] == "unavailable"
    assert "frozen protocol" in score["adapter_errors"][0]


def test_output_store_mismatch_cannot_turn_a_completed_witness_into_exported_proof(tmp_path, monkeypatch):
    run, _spec, _protocol, _retained, _closed, _base, export = prepare(tmp_path)
    monkeypatch.setattr(run.storage, "retrieve_output", lambda *_a: "foreign output")
    _paths, record, score = export()
    assert record["invocations"] == []
    assert score["status"] == "unavailable"
    assert "receipt-bound output" in score["adapter_errors"][0]


def test_unpublished_receipt_set_stays_unavailable(tmp_path, monkeypatch):
    run, _spec, _protocol, _retained, _closed, _base, export = prepare(tmp_path)
    monkeypatch.setattr(run.validator, "_current_scoped_receipts", lambda *_a: None)
    _paths, record, score = export()
    assert record["evaluation_identity"] is None
    assert not record["invocations"]
    assert score["status"] == "unavailable"


def test_observer_from_another_invocation_cannot_replace_freshness_evidence(tmp_path):
    run, _spec, _protocol, _retained, _closed, _base, export = prepare(tmp_path)
    run.fs.requirement_observer = SimpleNamespace(export_invocation=lambda *_a, **_k: {
        "run_id": run.state.run_id, "contract_id": "foreign", "artifacts": [],
        "reports_collection_complete": True,
    })
    _paths, record, score = export()
    assert not record["invocations"]
    assert score["status"] == "unavailable"
    assert "observer identity mismatch" in score["adapter_errors"][0]


def test_observer_can_supply_only_same_invocation_collections(tmp_path):
    run, _spec, _protocol, _retained, _closed, base, export = prepare(tmp_path)
    run.fs.requirement_observer = SimpleNamespace(export_invocation=lambda contract_id, **_k: {
        "run_id": run.state.run_id, "contract_id": contract_id,
        "effective_execution": {"inputs_complete": True, "serial": True, "wrapper_reviewed": True,
                                "argv": ["mvn", "clean", "install"], "maven_config": [], "maven_args": []},
        "reports": [], "reports_collection_complete": True, "artifacts": [], "errors": [],
    })
    _paths, record, score = export()
    invocation = load_json(bound_file(base, record["invocations"][0]))
    assert invocation["effective_execution"]["serial"] is True
    assert invocation["reports_collection_complete"] is True
    assert score["status"] == "unavailable"  # Scope/header and worktree still unproven.


def test_missing_observation_keeps_authorized_invocation_without_inventing_scope(tmp_path):
    run, _spec, _protocol, _retained, _closed, base, export = prepare(tmp_path)
    run.fs.requirement_observer = SimpleNamespace(export_invocation=lambda *_a, **_k: None)
    _paths, record, score = export()
    invocation = load_json(bound_file(base, record["invocations"][0]))
    assert invocation["reports_collection_complete"] is False
    assert invocation["effective_execution"]["inputs_complete"] is False
    assert "No boundary observation" in invocation["adapter_errors"][-1]
    assert score["status"] == "unavailable"


def test_closed_export_is_not_silently_replaced_by_a_later_view(tmp_path):
    _run, _spec, _protocol, _retained, _closed, _base, export = prepare(tmp_path)
    export()
    with pytest.raises(ValueError, match="already been closed"):
        export()


def test_finalizer_adds_host_sidecar_without_changing_existing_verdict(tmp_path):
    run, spec, protocol, _retained, closed, base, _export = prepare(tmp_path)
    run.fs.benchmark_requirements = spec
    run.fs.benchmark_evaluation_protocol = protocol
    run.fs.worktree_evidence_recorder = SimpleNamespace(directory=base / "worktree-evidence", run_id=run.state.run_id)
    finalizer = VerdictFinalizer(run.fs, validator=run.validator, project_name="proj",
                                repository=run.task.repo, acceptance_task=run.task,
                                output_storage=run.storage)
    snapshot = SimpleNamespace(task_completion=closed, verdict="success")
    finalizer._export_requirements_analysis(run.state, snapshot)
    assert snapshot.verdict == "success"
    assert run.fs.benchmark_analysis["status"] == "unavailable"
    assert bound_file(base, run.fs.benchmark_analysis["result"]).is_file()


def test_finalizer_retains_export_error_without_making_a_false_success(tmp_path):
    run, spec, protocol, _retained, closed, base, _export = prepare(tmp_path)
    run.fs.benchmark_requirements = spec
    run.fs.benchmark_evaluation_protocol = protocol
    run.fs.worktree_evidence_recorder = SimpleNamespace(directory=base / "worktree-evidence", run_id=run.state.run_id)
    finalizer = VerdictFinalizer(run.fs, validator=None, project_name="proj",
                                repository=run.task.repo, acceptance_task=run.task)
    snapshot = SimpleNamespace(task_completion=closed, verdict="partial")
    finalizer._export_requirements_analysis(run.state, snapshot)
    assert snapshot.verdict == "partial"
    assert run.fs.benchmark_analysis["status"] == "unavailable"


def test_starting_another_command_retires_previous_requirements_analysis():
    from sag.agent.agent import SetupAgent

    agent = object.__new__(SetupAgent)
    agent.run_id = "new-run"
    agent.orchestrator = SimpleNamespace(
        benchmark_analysis={"status": "complete"}, benchmark_requirements={"old": True},
        benchmark_evaluation_protocol={"old": True}, requirement_observer=object(),
        worktree_evidence_recorder=object(),
    )
    agent._clear_command_state()
    assert agent.orchestrator.benchmark_analysis is None
    assert agent.orchestrator.benchmark_requirements is None
    assert agent.orchestrator.benchmark_evaluation_protocol is None
    assert agent.orchestrator.requirement_observer is None
    assert agent.orchestrator.worktree_evidence_recorder is None


def test_real_close_exports_only_after_close_snapshot_and_verdict_publication(tmp_path, monkeypatch):
    from sag.agent.react_engine import ReActEngine
    from sag.agent.verdict_finalizer import EvidenceCloseReason, read_live_verdict_snapshot
    from sag.agent.worktree_evidence import WorktreeEvidenceRecorder
    import sag.benchmark.sag_adapter as adapter_module

    run, spec, protocol, _retained, _closed, base, _export = prepare(tmp_path)
    run.fs.benchmark_requirements = spec
    run.fs.benchmark_evaluation_protocol = protocol
    recorder = WorktreeEvidenceRecorder(base, run_id=run.state.run_id, project_root=ROOT,
                                       execute=run.fs.execute_command)
    run.fs.worktree_evidence_recorder = recorder
    recorder.capture("task_start")
    finalizer = VerdictFinalizer(run.fs, validator=run.validator, project_name="proj",
                                repository=run.task.repo, acceptance_task=run.task,
                                output_storage=run.storage)
    original = adapter_module.export_sag_requirements
    calls = []

    def checked_export(orchestrator, state, **kwargs):
        boundaries = {load_json(path)["boundary"] for path in recorder.directory.glob("*.json")}
        assert "evidence_close" in boundaries
        assert state.sealed
        assert read_live_verdict_snapshot(orchestrator).run_id == state.run_id
        calls.append(state.run_id)
        return original(orchestrator, state, **kwargs)

    monkeypatch.setattr(adapter_module, "export_sag_requirements", checked_export)
    engine = object.__new__(ReActEngine)
    # ContainerFS is a callable test-double API; the production orchestrator
    # is an object whose control method is callable, not a callable itself.
    engine.orchestrator = SimpleNamespace(worktree_evidence_recorder=recorder)
    engine.run_evidence_state, engine.verdict_finalizer = run.state, finalizer
    engine._await_open_obligations = lambda *_a: None
    engine._sweep_job_obligations = lambda: None
    engine._record_unsettled_job_conflicts = lambda *_a: None
    engine._emit_control_event = lambda *_a: None
    snapshot = engine._finalize_evidence(EvidenceCloseReason.TEST_TERMINATED)
    assert calls == [run.state.run_id]
    assert read_live_verdict_snapshot(run.fs) == snapshot
    assert bound_file(base, run.fs.benchmark_analysis["result"]).is_file()
    assert finalizer.finalize(run.state, EvidenceCloseReason.TEST_TERMINATED) == snapshot
    assert calls == [run.state.run_id]  # No post-close recollection on repeated reads.
