# tests/test_invocation_contracts.py
"""Plan 6 Stage B Task B1 — a frozen contract precedes every runner dispatch.

Spec §C3: the model submits one canonical public tool call; the facade
materializes it into an effective action and an exact cwd/argv, freezes that
materialization as an immutable `InvocationContract`, and dispatches ONLY if
the contract is on disk. Nothing about the physical call may be decided after
the fact, and a run whose contract could not be persisted has no authority to
touch the project at all.

The binding decision (plan §Stage B): the engine emits the action envelope
BEFORE tool execution and the materialized argv exists only inside the build
facade, so the freeze happens INSIDE `build_tool` — after the backend
materializes, strictly before the runner runs — and records the envelope id
that the verifier walks (envelope -> contract -> receipt).

Scripted-orchestrator style (house pattern, shared with
tests/test_invocation_receipts.py and tests/test_build_tool.py).
"""

import json
import shlex
from types import SimpleNamespace

import pytest
from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)

from sag.agent.control_events import canonical_json, canonical_sha256
from sag.agent.evidence_assessments import ASSESSMENT_DIR, ASSESSMENT_HEREDOC
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    CONTRACT_HEREDOC,
    CONTRACT_PERSIST_FAILED,
    CONTRACT_SCHEMA_VERSION,
    ActionContext,
    action_context,
    clear_action_context,
    compliance_class,
    contract_identity,
    contract_receipt_fields,
    current_action_context,
    current_contract,
    dispatch_contract,
    freeze_contract,
)
from sag.agent.invocation_receipts import RECEIPT_DIR, RECEIPT_HEREDOC, record_invocation
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool

DISPATCH = "<<runner dispatch>>"
SHA = "9f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"

# One surveyed manifest carrying the Stage A/§C2 projection the freeze pins.
MANIFEST = {
    "survey": {"config_fingerprint": "cfg-7", "project_path": "/workspace/proj"},
    "build_domains": [{"root": "/workspace/proj/core", "system": "maven"}],
    "domain_facts": [
        {
            "domain_id": "dom-abc123456789",
            "root": "/workspace/proj/core",
            "fact_epoch": 3,
            "open_conflicts": [
                {"kind": "version_incompatible", "edge_id": "edge-deadbeef0001"},
                {"kind": "partial_map", "path": "/workspace/proj/core/vendor"},
            ],
        }
    ],
}


class RecordingOrchestrator:
    """Records every container command in dispatch order.

    Answers the facade's marker probes, the freeze's target-sha probe and the
    atomic writes. `fail_contract_write` simulates the one failure the plan
    makes fatal: the contract cannot be persisted.
    """

    def __init__(self, markers=(), fail_contract_write=False, sha=SHA):
        self.markers = set(markers)
        self.fail_contract_write = fail_contract_write
        self.sha = sha
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "test -f" in command:
            tokens = shlex.split(command)
            probed = tokens[tokens.index("-f") + 1] if "-f" in tokens else ""
            exists = any(marker in probed for marker in self.markers)
            return {"success": True, "output": "exists" if exists else "missing"}
        if "rev-parse HEAD" in command:
            return {"success": True, "output": f"{self.sha}\n"}
        if CONTRACT_DIR in command and CONTRACT_HEREDOC in command:
            if self.fail_contract_write:
                return {"success": False, "output": "read-only file system"}
        return {"success": True, "output": ""}


class DispatchingBackendTool:
    """MavenTool/GradleTool/PythonTool stand-in that logs its own dispatch."""

    def __init__(self, orchestrator, result=None):
        self.orchestrator = orchestrator
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        self.orchestrator.commands.append(DISPATCH)
        return self.result


def _written(commands, directory, heredoc):
    """Every payload persisted through the recorded heredoc writes."""
    payloads = []
    for command in commands:
        if directory not in command or heredoc not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{heredoc}")
        payloads.append(json.loads(body))
    return payloads


def contracts_written(commands):
    return _written(commands, CONTRACT_DIR, CONTRACT_HEREDOC)


def assessments_written(commands):
    return _written(commands, ASSESSMENT_DIR, ASSESSMENT_HEREDOC)


def receipts_written(commands):
    return _written(commands, RECEIPT_DIR, RECEIPT_HEREDOC)


def _build_tool(markers, orchestrator=None, **tools):
    orchestrator = orchestrator or RecordingOrchestrator(markers)
    backends = {
        name: tools.get(name) or DispatchingBackendTool(orchestrator)
        for name in ("maven_tool", "gradle_tool", "python_tool")
    }
    return BuildTool(orchestrator, **backends), orchestrator, backends


@pytest.fixture(autouse=True)
def _no_leaked_action_context():
    """Every test starts with an empty request scope and leaves none behind."""
    clear_action_context()
    yield
    clear_action_context()


FREEZE_ARGS = {
    "envelope_id": "envelope-000012",
    "tool": "build",
    "params": {"action": "test", "working_directory": "/workspace/proj/core"},
    "effective_action": "verify",
    "expected_cwd": "/workspace/proj/core",
    "expected_argv": "--fail-at-end verify",
    "intent_source": "model",
    "requirements": MANIFEST,
}


# ---------------------------------------------------------------------------
# freeze_contract: shape, identity, persistence
# ---------------------------------------------------------------------------


def test_freeze_contract_records_the_v1_fields_and_persists_them_atomically():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    assert contract["schema_version"] == CONTRACT_SCHEMA_VERSION
    assert contract["envelope_id"] == "envelope-000012"
    assert contract["intent_source"] == "model"
    assert contract["requested_call"] == {
        "tool": "build",
        "params": {"action": "test", "working_directory": "/workspace/proj/core"},
    }
    assert contract["effective_action"] == "verify"
    assert contract["expected_cwd"] == "/workspace/proj/core"
    assert contract["expected_argv"] == "--fail-at-end verify"
    assert contract["target_sha"] == SHA
    assert contract["config_fingerprint"] == "cfg-7"
    assert contract["domain_id"] == "dom-abc123456789"
    assert contract["fact_epoch"] == 3
    # Only an edge that BLOCKS is a blocking conflict; an incomplete document
    # map is an unknown, not a block.
    assert contract["blocking_conflict_ids"] == ["edge-deadbeef0001"]

    (persisted,) = contracts_written(orchestrator.commands)
    assert persisted == contract
    write = next(c for c in orchestrator.commands if CONTRACT_HEREDOC in c)
    assert f"{CONTRACT_DIR}/{contract['contract_id']}.json.tmp" in write
    assert f"mv -f" in write


def test_contract_id_and_hash_recompute_from_the_persisted_payload():
    """Identity is derived, never assigned: a reader recomputes both."""
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    assert contract["contract_id"] == contract_identity("envelope-000012", "--fail-at-end verify")
    assert contract["contract_id"].startswith("ic-")
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    assert contract["contract_hash"] == canonical_sha256(body)


def test_frozen_contract_is_persisted_as_the_bytes_it_hashed():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    write = next(c for c in orchestrator.commands if CONTRACT_HEREDOC in c)
    assert canonical_json(contract) in write


def test_freeze_contract_omits_every_pin_it_does_not_know():
    """Absent facts are absent keys — never null, never a guessed default."""
    orchestrator = RecordingOrchestrator(sha="build log line, not a sha")

    contract = freeze_contract(
        orchestrator.execute_command,
        envelope_id="envelope-000001",
        tool="build",
        params={"action": "compile"},
        effective_action="compile",
        expected_cwd="/workspace/proj",
        expected_argv=None,
        intent_source="model",
        requirements={},
    )

    assert set(contract) == {
        "schema_version",
        "contract_id",
        "contract_hash",
        "envelope_id",
        "intent_source",
        "requested_call",
        "effective_action",
        "expected_cwd",
    }


def test_freeze_contract_records_a_predecessor_and_a_document_map_pin():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(
        orchestrator.execute_command,
        document_map_fingerprint="map-9",
        predecessor_contract_id="ic-000000000001",
        **FREEZE_ARGS,
    )

    assert contract["document_map_fingerprint"] == "map-9"
    assert contract["predecessor_contract_id"] == "ic-000000000001"


def test_freeze_contract_returns_none_when_the_write_fails():
    """A contract that is not on disk does not exist; the caller must refuse."""
    orchestrator = RecordingOrchestrator(fail_contract_write=True)

    assert freeze_contract(orchestrator.execute_command, **FREEZE_ARGS) is None


def test_freeze_contract_returns_none_when_the_container_is_gone():
    def execute(command, **kwargs):
        raise RuntimeError("container is gone")

    assert freeze_contract(execute, **FREEZE_ARGS) is None


def test_freeze_contract_refuses_a_call_the_canonical_form_cannot_hold():
    """An uncommittable call is not dispatched: the freeze fails closed."""
    orchestrator = RecordingOrchestrator()
    args = dict(FREEZE_ARGS, params={"action": "test", "sink": object()})

    assert freeze_contract(orchestrator.execute_command, **args) is None
    assert contracts_written(orchestrator.commands) == []


# ---------------------------------------------------------------------------
# compliance: the frozen argument vector versus the physical one
# ---------------------------------------------------------------------------


def test_compliance_is_exact_when_the_dispatch_ran_the_frozen_vector():
    assert compliance_class("--fail-at-end verify", "mvn --fail-at-end verify") == "exact"


def test_compliance_is_exact_across_pure_shell_quoting():
    assert compliance_class("--tests 'Foo Bar'", 'gradle --tests "Foo Bar"') == "exact"


def test_compliance_is_equivalent_when_the_runner_added_its_own_flags():
    """Every frozen token ran, in the frozen order; the runner added its own."""
    assert (
        compliance_class(
            "--fail-at-end verify",
            "/opt/maven/bin/mvn --fail-at-end -Dmaven.test.failure.ignore=true verify",
        )
        == "equivalent"
    )


def test_compliance_is_deviated_when_a_frozen_token_never_ran():
    assert compliance_class("test", "gradle --continue build") == "deviated"


def test_compliance_is_deviated_when_the_frozen_order_was_not_kept():
    assert compliance_class("-pl core compile", "mvn compile -pl core") == "deviated"


def test_compliance_is_deviated_when_the_dispatch_dropped_the_whole_vector():
    assert compliance_class("--fail-at-end verify", "mvn") == "deviated"


def test_compliance_is_unknown_when_either_side_states_no_argv():
    assert compliance_class(None, "python -m pytest") is None
    assert compliance_class("test", "") is None


# ---------------------------------------------------------------------------
# the request scope the facade reads the envelope identity from
# ---------------------------------------------------------------------------


def test_action_context_defaults_to_a_model_sourced_unbound_scope():
    assert current_action_context() == ActionContext(envelope_id=None, intent_source="model")


def test_action_context_is_restored_when_the_scope_closes():
    with action_context(envelope_id="forced-000003", intent_source="controller"):
        assert current_action_context().envelope_id == "forced-000003"
        assert current_action_context().intent_source == "controller"
    assert current_action_context().envelope_id is None


def test_contract_receipt_fields_are_absent_without_a_frozen_contract():
    assert contract_receipt_fields("mvn verify") == {}


def test_contract_receipt_fields_bind_the_receipt_to_the_contract():
    contract = {
        "contract_id": "ic-000000000abc",
        "contract_hash": "f" * 64,
        "expected_argv": "--fail-at-end verify",
    }

    with dispatch_contract(contract):
        assert current_contract() == contract
        assert contract_receipt_fields("mvn --fail-at-end verify") == {
            "contract_id": "ic-000000000abc",
            "contract_hash": "f" * 64,
            "compliance": "exact",
        }
    assert current_contract() is None


# ---------------------------------------------------------------------------
# build facade: the freeze precedes the dispatch, and gates it
# ---------------------------------------------------------------------------


def test_contract_is_frozen_before_the_runner_is_dispatched():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with action_context(envelope_id="envelope-000042"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert result.succeeded
    froze = orchestrator.commands.index(
        next(
            command
            for command in orchestrator.commands
            if CONTRACT_DIR in command and CONTRACT_HEREDOC in command
        )
    )
    assert froze < orchestrator.commands.index(DISPATCH)
    (contract,) = contracts_written(orchestrator.commands)
    assert contract["envelope_id"] == "envelope-000042"


def test_contract_records_the_cwd_the_facade_retargeted_to():
    """§C3: the requested call and the effective cwd stay separate facts — a
    normalization the caller never asked for is recorded, not smoothed over."""
    orchestrator = RecordingOrchestrator({"/workspace/proj/pom.xml"})
    orchestrator.project_name = "proj"
    tool, orchestrator, _ = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with action_context(envelope_id="envelope-000044"):
        tool.execute(action="compile")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["requested_call"]["params"]["working_directory"] == "/workspace"
    assert contract["expected_cwd"] == "/workspace/proj"


def test_the_result_carries_the_contract_identity_to_the_control_event():
    """The control event only ever sees the ToolResult metadata, and that is
    where the verifier picks the chain up (plan §Stage B)."""
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with action_context(envelope_id="envelope-000043"):
        result = tool.execute(action="compile", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert result.metadata["contract_id"] == contract["contract_id"]
    assert result.metadata["contract_hash"] == contract["contract_hash"]


def test_maven_contract_pins_the_materialized_lifecycle_and_cwd():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with action_context(envelope_id="envelope-000001"):
        tool.execute(action="package", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "package"
    assert contract["expected_argv"] == "--fail-at-end package -DskipTests"
    assert contract["expected_cwd"] == "/workspace/proj"
    assert contract["requested_call"] == {
        "tool": "build",
        "params": {"action": "package", "working_directory": "/workspace/proj"},
    }


def test_gradle_contract_pins_the_materialized_tasks():
    tool, orchestrator, _ = _build_tool({"build.gradle"})

    with action_context(envelope_id="envelope-000002"):
        tool.execute(action="package", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "assemble"
    assert contract["expected_argv"] == "--continue -x test assemble"


def test_python_contract_states_the_action_and_no_argv_it_cannot_know():
    """The venv interpreter and the junit path are resolved inside python_tool;
    the facade never guesses an argv it did not materialize."""
    tool, orchestrator, _ = _build_tool({"pyproject.toml"})

    with action_context(envelope_id="envelope-000003"):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "test"
    assert "expected_argv" not in contract


def test_contract_persistence_failure_refuses_the_dispatch():
    orchestrator = RecordingOrchestrator({"pom.xml"}, fail_contract_write=True)
    tool, orchestrator, backends = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with action_context(envelope_id="envelope-000009"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert not result.succeeded
    assert result.error_code == CONTRACT_PERSIST_FAILED
    assert backends["maven_tool"].calls == []
    assert DISPATCH not in orchestrator.commands
    (assessment,) = assessments_written(orchestrator.commands)
    assert assessment["stage"] == "materialization"
    assert assessment["typed_code"] == CONTRACT_PERSIST_FAILED
    assert assessment["event_or_intent_id"] == "envelope-000009"


def test_refused_dispatch_leaves_no_contract_bound_to_the_request():
    orchestrator = RecordingOrchestrator({"pom.xml"}, fail_contract_write=True)
    tool, _, _ = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with action_context(envelope_id="envelope-000010"):
        tool.execute(action="test", working_directory="/workspace/proj")

    assert current_contract() is None


def test_dispatch_unbinds_the_contract_when_the_facade_returns():
    tool, _, _ = _build_tool({"pom.xml"})

    with action_context(envelope_id="envelope-000011"):
        tool.execute(action="compile", working_directory="/workspace/proj")

    assert current_contract() is None


def test_forced_dispatch_freezes_a_controller_sourced_contract():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with action_context(envelope_id="forced-000007", intent_source="controller"):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["intent_source"] == "controller"
    assert contract["envelope_id"] == "forced-000007"


def test_a_dispatch_without_a_recorded_envelope_states_that_absence():
    """No engine envelope is a stated absence, not a borrowed identity."""
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    tool.execute(action="compile", working_directory="/workspace/proj")
    tool.execute(action="compile", working_directory="/workspace/proj")

    first, second = contracts_written(orchestrator.commands)
    assert first["envelope_id"].startswith("envelope-unrecorded-")
    assert first["intent_source"] == "model"
    # Two dispatches are two contracts even when nothing else differs.
    assert first["envelope_id"] != second["envelope_id"]
    assert first["contract_id"] != second["contract_id"]


# ---------------------------------------------------------------------------
# receipts carry the binding back
# ---------------------------------------------------------------------------


def _record(execute, argv):
    return record_invocation(
        execute,
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv=argv,
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        **contract_receipt_fields(argv),
    )


def test_receipt_carries_the_contract_identity_and_an_exact_compliance():
    orchestrator = RecordingOrchestrator()
    contract = {
        "contract_id": "ic-000000000abc",
        "contract_hash": "e" * 64,
        "expected_argv": "--fail-at-end verify",
    }

    with dispatch_contract(contract):
        _record(orchestrator.execute_command, "mvn --fail-at-end verify")

    (receipt,) = receipts_written(orchestrator.commands)
    assert receipt["contract_id"] == "ic-000000000abc"
    assert receipt["contract_hash"] == "e" * 64
    assert receipt["compliance"] == "exact"


def test_receipt_reports_a_dispatch_that_left_the_frozen_action():
    orchestrator = RecordingOrchestrator()
    contract = {
        "contract_id": "ic-000000000abc",
        "contract_hash": "e" * 64,
        "expected_argv": "--fail-at-end verify",
    }

    with dispatch_contract(contract):
        _record(orchestrator.execute_command, "mvn --fail-at-end package")

    (receipt,) = receipts_written(orchestrator.commands)
    assert receipt["compliance"] == "deviated"


def test_receipt_states_no_compliance_without_a_frozen_contract():
    orchestrator = RecordingOrchestrator()

    _record(orchestrator.execute_command, "mvn verify")

    (receipt,) = receipts_written(orchestrator.commands)
    assert "compliance" not in receipt
    assert "contract_id" not in receipt


def test_facade_dispatch_binds_its_receipt_to_the_contract_it_froze():
    """End to end through the facade: the receipt the runner writes carries the
    identity of the contract the facade froze for that same dispatch."""
    orchestrator = RecordingOrchestrator({"pom.xml"})
    seen = {}

    class ReceiptWritingTool:
        def __init__(self):
            self.orchestrator = orchestrator
            self.calls = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            seen.update(contract_receipt_fields("mvn --fail-at-end verify"))
            return ToolResult.completed_success(output="BUILD SUCCESS")

    tool = BuildTool(orchestrator, maven_tool=ReceiptWritingTool())

    with action_context(envelope_id="envelope-000021"):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert seen == {
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "compliance": "exact",
    }


def test_the_real_maven_dispatch_runs_the_vector_its_contract_froze():
    """Drift guard over the WHOLE chain with the real MavenTool: the facade
    predicts an argument vector, the runner builds the physical command, and
    the receipt states how the two relate. If either side moves, the
    compliance class moves with it and this fails loudly."""
    from test_build_tool_preflight_integration import EndToEndOrch, _e2e_build_tool

    orch = EndToEndOrch([(True, "BUILD SUCCESS")], java="17", manifest={"java_version": "17"})

    with action_context(envelope_id="envelope-000099"):
        result = _e2e_build_tool(orch).execute(
            action="compile",
            working_directory="/workspace/proj",
        )

    assert result.succeeded
    (contract,) = contracts_written(orch.commands)
    (receipt,) = receipts_written(orch.commands)
    assert contract["expected_argv"] == "--fail-at-end compile"
    assert receipt["argv"] == "mvn --fail-at-end compile"
    assert receipt["contract_id"] == contract["contract_id"]
    assert receipt["contract_hash"] == contract["contract_hash"]
    assert receipt["compliance"] == "exact"


def test_the_gradle_vector_the_facade_freezes_is_the_one_the_runner_builds():
    """Same drift guard for gradle, at the seam where the two token orders
    have to agree (`GradleBackend.expected_argv` mirrors
    `GradleTool._build_gradle_command`)."""
    from sag.tools.build.backends import GradleBackend
    from sag.tools.internal.gradle_tool import GradleTool

    backend = GradleBackend(SimpleNamespace(orchestrator=None))
    params = backend.materialize("package", "--info", "/workspace/proj", None)
    physical = GradleTool._build_gradle_command(
        None,
        "gradle",
        params["tasks"],
        "",
        params.get("gradle_args"),
        "",
        False,
        False,
        False,
        params.get("fail_at_end", False),
    )

    assert params["gradle_args"] == "--info -x test"
    assert compliance_class(backend.expected_argv(params), physical) == "exact"


# ---------------------------------------------------------------------------
# the engine publishes the identity the facade reads
# ---------------------------------------------------------------------------


def test_action_envelope_emission_opens_the_request_scope(tmp_path):
    from sag.agent.control_events import ControlEventSink
    from sag.agent.react_engine import ReActEngine
    from sag.agent.react_types import StepType

    engine = object.__new__(ReActEngine)
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine._active_control_envelope_id = None
    engine.steps = [SimpleNamespace(step_type=StepType.ACTION, tool_call_id="call-1")]

    envelope_id = engine._emit_control_action_envelope("build", {"action": "test"})

    assert current_action_context() == ActionContext(
        envelope_id=envelope_id,
        intent_source="model",
    )


def test_a_sinkless_engine_opens_no_scope_for_the_next_dispatch(tmp_path):
    from sag.agent.react_engine import ReActEngine

    engine = object.__new__(ReActEngine)
    engine.control_event_sink = None
    engine._active_control_envelope_id = None
    engine.steps = []

    with action_context(envelope_id="envelope-000030"):
        assert engine._emit_control_action_envelope("build", {}) is None
        assert current_action_context().envelope_id is None


def test_forced_test_attempt_runs_under_a_controller_scope(forced_engine):
    """A harness-forced attempt is the controller's intent, and its contract
    must say so — the model never authored that call."""
    engine, requirement = forced_engine()
    dispatched = engine._execute_tool_call
    seen = {}

    def capture(call):
        seen["context"] = current_action_context()
        return dispatched(call)

    engine._execute_tool_call = capture

    assert engine._force_required_test_attempt(requirement, trigger="phase_floor") is True

    assert seen["context"] == ActionContext(
        envelope_id="forced-000001",
        intent_source="controller",
    )
    assert current_action_context().envelope_id is None
