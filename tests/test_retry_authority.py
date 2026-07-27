# tests/test_retry_authority.py
"""Plan 6 Stage D Task D1 — the material-progress retry law.

Spec §C7: a deterministic failure may not be repeated identically. Recurrence
gets an identity (`retry_key`), the CONTROLLER signs it after a failure-class
assessment closes a dispatch, and the FACADE validates it before it freezes the
next contract — it keeps no second recurrence state of its own.

Five properties are asserted here rather than assumed:

* the key is STABLE over the same facts and ORDER-SENSITIVE over the argv
  vector, because `-pl core test` and `test -pl core` are two builds;
* the ledger survives its own failure modes: the write is atomic, a corrupt
  file is read as empty rather than crashing a run, and an unwritable
  workspace refuses rather than raises;
* an identical deterministic retry is REFUSED, and each of the four named
  deltas — argv, environment, accepted repair, fact epoch — is allowed;
* transient network/timeout failures spend a budget instead of a delta;
* lifecycle is not a retry: a detached handoff records nothing, so nothing
  refuses the dispatch that eventually observes it.

Scripted-orchestrator style (house pattern, shared with
tests/test_invocation_contracts.py and tests/test_repair_contracts.py).
"""

import json
import shlex

import pytest
from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)

from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    CONTRACT_HEREDOC,
    build_contract,
    clear_action_context,
    freeze_contract,
)
from sag.agent.react_types import ReActStep, StepType
from sag.agent.repair_contracts import clear_accepted_repair, set_accepted_repair
from sag.agent.retry_authority import (
    RETRY_LEDGER_HEREDOC,
    RETRY_LEDGER_PATH,
    RETRY_WITHOUT_DELTA,
    RETRY_WITHOUT_DELTA_CODE,
    TRANSIENT_RETRY_BUDGET,
    argv_tokens,
    blocking_entry,
    candidate_contract,
    compute_retry_key,
    failure_codes,
    material_delta,
    normalized_action,
    read_ledger,
    record_failure,
    transient_allowance,
    write_ledger,
)
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

SHA = "9f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
OTHER_SHA = "1122334455667788990011223344556677889900"
DOMAIN = "/workspace/proj/core"
DISPATCH = "<<runner dispatch>>"

MANIFEST = {
    "survey": {"config_fingerprint": "cfg-7", "project_path": "/workspace/proj"},
    "build_domains": [{"root": DOMAIN, "system": "maven"}],
    "domain_facts": [{"domain_id": "dom-abc123456789", "root": DOMAIN, "fact_epoch": 3}],
}

FREEZE_ARGS = {
    "envelope_id": "envelope-000012",
    "tool": "build",
    "params": {"action": "test", "working_directory": DOMAIN},
    "effective_action": "verify",
    "expected_cwd": DOMAIN,
    "expected_argv": "-pl core verify",
    "intent_source": "model",
    "requirements": MANIFEST,
}


# ---------------------------------------------------------------------------
# scripted container
# ---------------------------------------------------------------------------


def ok(output=""):
    return {"success": True, "output": output}


def fail(output=""):
    return {"success": False, "output": output}


class ContainerFS:
    """Execute double with a file layer, so atomic rewrites are observable.

    The four shapes this lane's writers and readers issue: the marker probe,
    the target-sha probe, a single-path or glob `cat`, and the
    `mkdir -p … && cat > tmp <<HEREDOC && mv -f tmp final` rewrite.
    """

    def __init__(self, files=None, markers=(), sha=SHA, writable=True):
        self.files = dict(files or {})
        self.markers = set(markers)
        self.sha = sha
        self.writable = writable
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "test -f" in command:
            tokens = shlex.split(command)
            probed = tokens[tokens.index("-f") + 1] if "-f" in tokens else ""
            exists = any(marker in probed for marker in self.markers)
            return ok("exists" if exists else "missing")
        if "rev-parse HEAD" in command:
            return ok(f"{self.sha}\n")
        if command.startswith("cat ") and "\n" not in command:
            target = shlex.split(command)[1]
            if target.endswith("/*.json"):
                prefix = target[: -len("*.json")]
                return ok(
                    "\n".join(
                        body
                        for path, body in sorted(self.files.items())
                        if path.startswith(prefix) and path.endswith(".json")
                    )
                )
            if target in self.files:
                return ok(self.files[target])
            return fail(f"cat: {target}: No such file or directory")
        if "mv -f " in command and "\n" in command:
            if not self.writable:
                return fail("Read-only file system")
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            final = header.rsplit("mv -f ", 1)[1].split()[1]
            self.files[final] = body
            return ok("")
        return ok("")

    def writes(self):
        return [command for command in self.commands if "mv -f " in command]

    def ledger_writes(self):
        return [command for command in self.commands if RETRY_LEDGER_HEREDOC in command]


class ScriptedOrchestrator:
    """`execute_command` over a `ContainerFS` (the engine's read surface)."""

    project_name = "proj"

    def __init__(self, **kwargs):
        self.filesystem = ContainerFS(**kwargs)

    def execute_command(self, command, **kwargs):
        return self.filesystem(command)

    def read_file(self, path):
        """The manifest read surface (`MarkerOrchestrator`'s shape)."""
        body = self.filesystem.files.get(path)
        if body is None:
            return {"success": False, "content": "", "exit_code": 1}
        return {"success": True, "content": body, "exit_code": 0}


class DispatchingBackendTool:
    """MavenTool/GradleTool/PythonTool stand-in that logs its own dispatch."""

    def __init__(self, orchestrator, result=None):
        self.orchestrator = orchestrator
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        self.orchestrator.filesystem.commands.append(DISPATCH)
        return self.result


def build_tool(orchestrator, **tools):
    backends = {
        name: tools.get(name) or DispatchingBackendTool(orchestrator)
        for name in ("maven_tool", "gradle_tool", "python_tool")
    }
    return BuildTool(orchestrator, **backends)


def maven_orchestrator(manifest=None, **kwargs):
    kwargs.setdefault("markers", {"pom.xml"})
    orchestrator = ScriptedOrchestrator(**kwargs)
    orchestrator.filesystem.files[REQUIREMENTS_PATH] = json.dumps(manifest or MANIFEST)
    return orchestrator


def written_contracts(orchestrator):
    payloads = []
    for command in orchestrator.filesystem.commands:
        if CONTRACT_DIR not in command or CONTRACT_HEREDOC not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{CONTRACT_HEREDOC}")
        payloads.append(json.loads(body))
    return payloads


def assessment(typed_code, *, receipt_id="rcpt-maven-0001"):
    """A persisted `ReceiptAssessment` body (lane z1's shape)."""
    return {
        "schema_version": 1,
        "assessment_id": f"asm-{receipt_id}-{typed_code}-0000abcd",
        "receipt_id": receipt_id,
        "typed_code": typed_code,
    }


@pytest.fixture(autouse=True)
def _no_leaked_request_scope():
    """Every test starts with an empty request scope and leaves none behind."""
    clear_action_context()
    clear_accepted_repair()
    yield
    clear_action_context()
    clear_accepted_repair()


# ---------------------------------------------------------------------------
# identity: the plan's retry_key formula, verbatim
# ---------------------------------------------------------------------------


def test_the_same_dispatch_and_the_same_typed_code_hash_the_same_key():
    orchestrator = ScriptedOrchestrator()
    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    first = compute_retry_key(contract, "expectation_unmet")
    second = compute_retry_key(dict(contract), "expectation_unmet")

    assert first == second
    assert len(first) == 16
    assert all(character in "0123456789abcdef" for character in first)


def test_the_typed_code_is_part_of_the_identity():
    orchestrator = ScriptedOrchestrator()
    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    assert compute_retry_key(contract, "expectation_unmet") != compute_retry_key(
        contract, "timeout"
    )


def test_argv_tokens_are_ordered_not_sorted():
    """`argv_tokens_sorted:false` — the same tokens in another order are
    another dispatch, so the key must move."""
    orchestrator = ScriptedOrchestrator()
    forward = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)
    reversed_argv = freeze_contract(
        orchestrator.execute_command, **{**FREEZE_ARGS, "expected_argv": "verify -pl core"}
    )

    assert argv_tokens("-pl core verify") == ["-pl", "core", "verify"]
    assert normalized_action(forward)["argv_tokens"] == ["-pl", "core", "verify"]
    assert normalized_action(reversed_argv)["argv_tokens"] == ["verify", "-pl", "core"]
    assert compute_retry_key(forward, "expectation_unmet") != compute_retry_key(
        reversed_argv, "expectation_unmet"
    )


def test_every_keyed_fact_moves_the_key():
    orchestrator = ScriptedOrchestrator()
    base = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)
    key = compute_retry_key(base, "expectation_unmet")

    moved = ScriptedOrchestrator(sha=OTHER_SHA)
    moved_contract = freeze_contract(moved.execute_command, **FREEZE_ARGS)
    assert compute_retry_key(moved_contract, "expectation_unmet") != key
    assert compute_retry_key({**base, "domain_id": "dom-other"}, "expectation_unmet") != key
    assert compute_retry_key({**base, "config_fingerprint": "cfg-9"}, "expectation_unmet") != key
    assert compute_retry_key({**base, "effective_action": "install"}, "expectation_unmet") != key


def test_prose_state_is_not_part_of_the_identity():
    """Spec §C7: changing prose is not material progress, so a re-indexed
    document map may not become a licence to repeat a failed build."""
    orchestrator = ScriptedOrchestrator()
    contract = freeze_contract(
        orchestrator.execute_command, document_map_fingerprint="map-1", **FREEZE_ARGS
    )
    reindexed = {**contract, "document_map_fingerprint": "map-2"}

    assert compute_retry_key(contract, "expectation_unmet") == compute_retry_key(
        reindexed, "expectation_unmet"
    )


def test_an_unknown_pin_is_an_absent_key_never_a_null():
    orchestrator = ScriptedOrchestrator(sha="build log line, not a sha")
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

    assert "target_sha" not in contract
    assert normalized_action(contract)["argv_tokens"] == []
    # A key is still derivable — an unstated pin contributes nothing, and two
    # dispatches that state the same nothing still collide honestly.
    assert compute_retry_key(contract, "expectation_unmet") == compute_retry_key(
        build_contract(
            envelope_id="envelope-000002",
            tool="build",
            params={"action": "compile"},
            effective_action="compile",
            expected_cwd="/workspace/proj",
            intent_source="model",
        ),
        "expectation_unmet",
    )


def test_the_pre_freeze_candidate_reproduces_the_frozen_key():
    """The facade validates BEFORE the freeze, so the candidate view and the
    contract the freeze produces for the same dispatch must key identically —
    otherwise the law is silently inert."""
    orchestrator = ScriptedOrchestrator()
    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    candidate = candidate_contract(
        orchestrator.execute_command,
        tool="build",
        effective_action="verify",
        expected_cwd=DOMAIN,
        expected_argv="-pl core verify",
        requirements=MANIFEST,
    )

    assert compute_retry_key(candidate, "expectation_unmet") == compute_retry_key(
        contract, "expectation_unmet"
    )
    assert candidate["fact_epoch"] == contract["fact_epoch"]
    assert candidate["intent_source"] == contract["intent_source"]


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def test_a_recorded_failure_is_persisted_atomically_in_the_plan_s_shape():
    orchestrator = ScriptedOrchestrator()

    entry = record_failure(orchestrator.execute_command, "key-1", "ic-abc123abc123", "timeout")

    assert entry == {"count": 1, "typed_code": "timeout", "last_contract_id": "ic-abc123abc123"}
    write = orchestrator.filesystem.ledger_writes()[-1]
    assert f"{RETRY_LEDGER_PATH}.tmp" in write
    assert " && mv -f " in write
    assert json.loads(orchestrator.filesystem.files[RETRY_LEDGER_PATH]) == {"key-1": entry}


def test_the_count_accumulates_per_key_and_the_last_contract_wins():
    orchestrator = ScriptedOrchestrator()

    record_failure(orchestrator.execute_command, "key-1", "ic-first", "expectation_unmet")
    record_failure(orchestrator.execute_command, "key-1", "ic-second", "expectation_unmet")
    record_failure(orchestrator.execute_command, "key-2", "ic-third", "expectation_unmet")

    ledger = read_ledger(orchestrator.execute_command)
    assert ledger["key-1"] == {
        "count": 2,
        "typed_code": "expectation_unmet",
        "last_contract_id": "ic-second",
    }
    assert ledger["key-2"]["count"] == 1


def test_a_corrupt_ledger_is_read_as_empty_and_never_raises():
    orchestrator = ScriptedOrchestrator(files={RETRY_LEDGER_PATH: "{not json at all"})

    assert read_ledger(orchestrator.execute_command) == {}
    # And the next record rewrites it rather than inheriting the damage.
    assert record_failure(orchestrator.execute_command, "key-1", "ic-1", "timeout") == {
        "count": 1,
        "typed_code": "timeout",
        "last_contract_id": "ic-1",
    }


def test_a_ledger_that_is_not_a_key_map_is_read_as_empty():
    orchestrator = ScriptedOrchestrator(files={RETRY_LEDGER_PATH: json.dumps(["key-1"])})

    assert read_ledger(orchestrator.execute_command) == {}


def test_an_absent_ledger_is_no_ledger_not_a_failure():
    orchestrator = ScriptedOrchestrator()

    assert read_ledger(orchestrator.execute_command) == {}


def test_a_ledger_that_cannot_be_written_is_reported_rather_than_raised():
    orchestrator = ScriptedOrchestrator(writable=False)

    assert record_failure(orchestrator.execute_command, "key-1", "ic-1", "timeout") is None
    assert write_ledger(orchestrator.execute_command, {"key-1": {"count": 1}}) is False


def test_a_keyless_or_codeless_record_writes_nothing():
    orchestrator = ScriptedOrchestrator()

    assert record_failure(orchestrator.execute_command, "", "ic-1", "timeout") is None
    assert record_failure(orchestrator.execute_command, "key-1", "ic-1", "") is None
    assert orchestrator.filesystem.ledger_writes() == []


# ---------------------------------------------------------------------------
# the law: material delta and the transient budget
# ---------------------------------------------------------------------------


def _prior(**overrides):
    return {
        "expected_argv": "-pl core verify",
        "config_fingerprint": "cfg-7",
        "fact_epoch": 3,
        "intent_source": "model",
        **overrides,
    }


ENTRY = {"count": 1, "typed_code": "expectation_unmet", "last_contract_id": "ic-1"}


def test_an_identical_candidate_states_no_material_delta():
    assert material_delta(_prior(), ENTRY, _prior()) is False


def test_each_named_delta_is_material():
    assert material_delta(_prior(expected_argv="-pl core -X verify"), ENTRY, _prior()) is True
    assert material_delta(_prior(config_fingerprint="cfg-9"), ENTRY, _prior()) is True
    assert material_delta(_prior(intent_source="accepted_repair"), ENTRY, _prior()) is True
    assert material_delta(_prior(fact_epoch=4), ENTRY, _prior()) is True


def test_a_reordered_argv_is_a_material_delta():
    assert material_delta(_prior(expected_argv="verify -pl core"), ENTRY, _prior()) is True


def test_a_fact_only_one_side_states_is_unknown_never_a_change():
    absent_prior = {"expected_argv": "-pl core verify", "config_fingerprint": "cfg-7"}
    assert material_delta(_prior(), ENTRY, absent_prior) is False
    assert material_delta(absent_prior, ENTRY, _prior()) is False


def test_an_unreadable_prior_contract_is_not_a_delta():
    """An unreadable file must never become a licence to repeat — only the
    repair stamp, which the candidate states itself, survives it."""
    assert material_delta(_prior(), ENTRY, None) is False
    assert material_delta(_prior(intent_source="accepted_repair"), ENTRY, None) is True


def test_transient_codes_spend_a_budget_and_then_refuse():
    assert TRANSIENT_RETRY_BUDGET == 2
    assert transient_allowance("transient_network", 0) is True
    assert transient_allowance("transient_network", 1) is True
    assert transient_allowance("transient_network", 2) is False
    assert transient_allowance("timeout", 1) is True
    assert transient_allowance("timeout", 2) is False


def test_a_deterministic_code_has_no_transient_budget():
    assert transient_allowance("expectation_unmet", 0) is False
    assert transient_allowance("falsifier_empty_delta_despite_success", 0) is False


# ---------------------------------------------------------------------------
# the facade: refusal before the freeze
# ---------------------------------------------------------------------------


def _first_dispatch(orchestrator, **overrides):
    """Run one build and return the contract it froze."""
    tool = build_tool(orchestrator)
    call = {"action": "test", "working_directory": DOMAIN, **overrides}
    tool.execute(**call)
    return written_contracts(orchestrator)[-1]


def _remember(orchestrator, contract, typed_code, times=1):
    key = compute_retry_key(contract, typed_code)
    for _ in range(times):
        record_failure(orchestrator.execute_command, key, contract["contract_id"], typed_code)
    return key


def test_the_facade_dispatch_states_the_pins_the_delta_rules_read():
    """Guard against a vacuous suite: with an unpinned contract every "changed
    X is allowed" case below would pass for the wrong reason."""
    orchestrator = maven_orchestrator()

    contract = _first_dispatch(orchestrator)

    assert contract["target_sha"] == SHA
    assert contract["domain_id"] == "dom-abc123456789"
    assert contract["config_fingerprint"] == "cfg-7"
    assert contract["fact_epoch"] == 3
    assert contract["expected_argv"] == "--fail-at-end verify"


def test_an_identical_deterministic_retry_is_refused_before_the_freeze():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "expectation_unmet")
    frozen_before = len(written_contracts(orchestrator))
    orchestrator.filesystem.commands.clear()

    result = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    assert result.error_code == RETRY_WITHOUT_DELTA
    assert result.operation_outcome.value == "failed"
    # Named in the output: the prior typed code and how often it happened.
    assert "expectation_unmet" in result.output
    assert "×1" in result.output
    # Nothing ran and nothing was frozen for it.
    assert DISPATCH not in orchestrator.filesystem.commands
    assert len(written_contracts(orchestrator)) == 0
    assert frozen_before == 1


def test_the_refusal_records_a_typed_control_assessment():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "expectation_unmet")

    build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    assessments = [
        json.loads(body)
        for path, body in orchestrator.filesystem.files.items()
        if path.startswith(ASSESSMENT_DIR)
    ]
    refusal = next(
        record for record in assessments if record["typed_code"] == RETRY_WITHOUT_DELTA_CODE
    )
    assert refusal["stage"] == "precondition"
    assert "expectation_unmet" in refusal["detail"]


def test_a_changed_argv_is_allowed_through():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "expectation_unmet")
    orchestrator.filesystem.commands.clear()

    result = build_tool(orchestrator).execute(
        action="test", working_directory=DOMAIN, args="-DskipITs"
    )

    assert result.error_code != RETRY_WITHOUT_DELTA
    assert DISPATCH in orchestrator.filesystem.commands


def test_an_accepted_repair_is_allowed_through():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "expectation_unmet")
    orchestrator.filesystem.commands.clear()
    set_accepted_repair("rep-abc123abc123")

    result = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    assert result.error_code != RETRY_WITHOUT_DELTA
    assert DISPATCH in orchestrator.filesystem.commands


def test_a_changed_fact_epoch_is_allowed_through():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "expectation_unmet")
    moved = dict(MANIFEST)
    moved["domain_facts"] = [{**MANIFEST["domain_facts"][0], "fact_epoch": 4}]
    orchestrator.filesystem.files[REQUIREMENTS_PATH] = json.dumps(moved)
    orchestrator.filesystem.commands.clear()

    result = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    assert result.error_code != RETRY_WITHOUT_DELTA
    assert DISPATCH in orchestrator.filesystem.commands


def test_a_transient_failure_is_repeated_inside_its_budget_and_then_refused():
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    _remember(orchestrator, contract, "transient_network")

    inside = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)
    assert inside.error_code != RETRY_WITHOUT_DELTA

    _remember(orchestrator, contract, "transient_network")
    orchestrator.filesystem.commands.clear()

    exhausted = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)
    assert exhausted.error_code == RETRY_WITHOUT_DELTA
    assert "transient_network" in exhausted.output
    assert DISPATCH not in orchestrator.filesystem.commands


def test_a_different_typed_failure_of_the_same_action_does_not_refuse_it():
    """The ledger is keyed by the typed CAUSE: a dispatch that timed out says
    nothing about whether the same argv can compile."""
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    record_failure(
        orchestrator.execute_command,
        compute_retry_key(contract, "expectation_unmet"),
        contract["contract_id"],
        "expectation_unmet",
    )
    orchestrator.filesystem.commands.clear()

    result = build_tool(orchestrator).execute(action="compile", working_directory=DOMAIN)

    assert result.error_code != RETRY_WITHOUT_DELTA
    assert DISPATCH in orchestrator.filesystem.commands


def test_an_empty_ledger_never_reads_a_contract():
    orchestrator = maven_orchestrator()
    candidate = candidate_contract(
        orchestrator.execute_command,
        tool="build",
        effective_action="verify",
        expected_cwd=DOMAIN,
        expected_argv="-pl core verify",
        requirements=MANIFEST,
    )

    assert blocking_entry(orchestrator.execute_command, candidate) is None


def test_a_first_dispatch_costs_one_ledger_read_and_no_second_probe():
    """The law is free on the path it does not govern: with nothing recorded
    the facade never builds a candidate, so the freeze's target-sha probe stays
    the only one."""
    orchestrator = maven_orchestrator()

    build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    commands = orchestrator.filesystem.commands
    assert len([c for c in commands if "rev-parse HEAD" in c]) == 1
    assert len([c for c in commands if RETRY_LEDGER_PATH in c]) == 1


# ---------------------------------------------------------------------------
# the controller: signing the key at the observation seam
# ---------------------------------------------------------------------------


def _observing_engine(factory, orchestrator, result):
    engine, _ = factory()
    engine.orchestrator = orchestrator
    engine.steps = [
        ReActStep(
            step_type=StepType.ACTION,
            content="build",
            tool_name="build",
            tool_params={"action": "test"},
            tool_result=result,
            timestamp="2026-07-26T00:00:00Z",
            tool_call_id="call-1",
        )
    ]
    return engine


def _dispatched(orchestrator, *, typed_code="expectation_unmet", metadata=None):
    """One frozen contract plus one persisted assessment of its receipt."""
    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)
    orchestrator.filesystem.files[f"{CONTRACT_DIR}/{contract['contract_id']}.json"] = json.dumps(
        contract, sort_keys=True
    )
    verdict = assessment(typed_code)
    orchestrator.filesystem.files[f"{ASSESSMENT_DIR}/{verdict['assessment_id']}.json"] = json.dumps(
        verdict, sort_keys=True
    )
    result = ToolResult.completed_failure(
        output="BUILD FAILURE",
        error_code="BUILD_FAILED",
        metadata={
            "receipt_id": "rcpt-maven-0001",
            "contract_id": contract["contract_id"],
            **(metadata or {}),
        },
    )
    return contract, result


def test_the_engine_records_the_retry_key_of_an_assessed_failure(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    contract, result = _dispatched(orchestrator)
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    ledger = read_ledger(orchestrator.execute_command)
    assert ledger == {
        compute_retry_key(contract, "expectation_unmet"): {
            "count": 1,
            "typed_code": "expectation_unmet",
            "last_contract_id": contract["contract_id"],
        }
    }


def test_the_engine_records_nothing_for_a_passing_dispatch(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    _dispatched(orchestrator, typed_code="expectation_met")
    _, result = _dispatched(orchestrator, typed_code="expectation_met")
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "BUILD SUCCESS", source_tool="build")

    assert read_ledger(orchestrator.execute_command) == {}


def test_a_capability_absence_alone_is_not_a_retried_failure(forced_engine):  # noqa: F811
    """A green suite that skipped its LLVM cases is still green; recording the
    rider would refuse the next run of a suite that passed."""
    orchestrator = ScriptedOrchestrator()
    _, result = _dispatched(orchestrator, typed_code="capability_absent_llvm")
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "50 passed, 2 skipped", source_tool="build")

    assert read_ledger(orchestrator.execute_command) == {}


def test_a_detached_handoff_is_lifecycle_not_a_retry(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    _, result = _dispatched(orchestrator, metadata={"dispatch_status": "running_detached"})
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "job:abc handed off", source_tool="build")

    assert read_ledger(orchestrator.execute_command) == {}


def test_the_engine_records_nothing_for_a_non_build_observation(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    _, result = _dispatched(orchestrator)
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "analyzed", source_tool="project")

    assert read_ledger(orchestrator.execute_command) == {}


def test_the_engine_records_nothing_when_the_result_names_no_contract(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    result = ToolResult.completed_failure(
        output="refused", error_code="ARGS_INVALID", metadata={"receipt_id": "rcpt-maven-0001"}
    )
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "refused", source_tool="build")

    assert read_ledger(orchestrator.execute_command) == {}


def test_a_second_observation_of_the_same_failure_accumulates(forced_engine):  # noqa: F811
    orchestrator = ScriptedOrchestrator()
    contract, result = _dispatched(orchestrator)
    engine = _observing_engine(forced_engine, orchestrator, result)

    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")
    engine._append_native_observation("call-2", "BUILD FAILURE", source_tool="build")

    ledger = read_ledger(orchestrator.execute_command)
    assert ledger[compute_retry_key(contract, "expectation_unmet")]["count"] == 2


def test_only_this_receipt_s_failure_codes_are_signed():
    orchestrator = ScriptedOrchestrator()
    for record in (
        assessment("expectation_unmet"),
        assessment("expectation_met", receipt_id="rcpt-maven-0002"),
        assessment("capability_absent_llvm"),
    ):
        orchestrator.filesystem.files[f"{ASSESSMENT_DIR}/{record['assessment_id']}.json"] = (
            json.dumps(record, sort_keys=True)
        )

    assert failure_codes(orchestrator.execute_command, "rcpt-maven-0001") == ["expectation_unmet"]
    assert failure_codes(orchestrator.execute_command, "rcpt-maven-0002") == []
    assert failure_codes(orchestrator.execute_command, "") == []


def test_the_controller_and_the_facade_agree_on_one_key(forced_engine):  # noqa: F811
    """End to end: what the controller signs is exactly what the facade
    refuses — the two must never keep separate recurrence state."""
    orchestrator = maven_orchestrator()
    contract = _first_dispatch(orchestrator)
    verdict = assessment("expectation_unmet")
    orchestrator.filesystem.files[f"{ASSESSMENT_DIR}/{verdict['assessment_id']}.json"] = json.dumps(
        verdict, sort_keys=True
    )
    orchestrator.filesystem.files[f"{CONTRACT_DIR}/{contract['contract_id']}.json"] = json.dumps(
        contract, sort_keys=True
    )
    result = ToolResult.completed_failure(
        output="BUILD FAILURE",
        error_code="BUILD_FAILED",
        metadata={"receipt_id": "rcpt-maven-0001", "contract_id": contract["contract_id"]},
    )
    engine = _observing_engine(forced_engine, orchestrator, result)
    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    refused = build_tool(orchestrator).execute(action="test", working_directory=DOMAIN)

    assert refused.error_code == RETRY_WITHOUT_DELTA


class _RegistryOrch:
    """Answers the toolchain-registry cat with configurable content."""

    def __init__(self, registry_body=""):
        self.registry_body = registry_body

    def __call__(self, command, **_kwargs):
        if "toolchains.json" in command:
            if self.registry_body is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.registry_body}
        return {"success": True, "exit_code": 0, "output": ""}


def test_a_toolchain_registration_changes_the_retry_identity():
    """Live p6v-cli-r1: Maven 3.8.7 failed the version gate, the harness
    registered 3.9.9, and the retry of the SAME compile was refused as
    RETRY_WITHOUT_DELTA — the registry change is toolchain state, and spec
    §C7 names changed toolchain state a material difference."""
    from sag.agent.retry_authority import compute_retry_key, toolchain_state_fingerprint

    contract = {
        "requested_call": {"tool": "build"},
        "effective_action": "compile",
        "expected_argv": "--fail-at-end compile",
        "config_fingerprint": "cfg-1",
    }
    before = toolchain_state_fingerprint(_RegistryOrch("{}"))
    after = toolchain_state_fingerprint(
        _RegistryOrch('{"maven": {"3.9.9": "/workspace/apache-maven-3.9.9"}}')
    )

    key_before = compute_retry_key(contract, "expectation_unmet", toolchain_state=before)
    key_after = compute_retry_key(contract, "expectation_unmet", toolchain_state=after)

    assert key_before != key_after


def test_an_unchanged_registry_keeps_the_retry_identity():
    from sag.agent.retry_authority import compute_retry_key, toolchain_state_fingerprint

    contract = {"requested_call": {"tool": "build"}, "effective_action": "compile"}
    state = toolchain_state_fingerprint(_RegistryOrch('{"maven": {}}'))

    assert compute_retry_key(contract, "expectation_unmet", toolchain_state=state) == \
        compute_retry_key(contract, "expectation_unmet", toolchain_state=state)


def test_an_absent_registry_contributes_no_key_material():
    from sag.agent.retry_authority import compute_retry_key, toolchain_state_fingerprint

    contract = {"requested_call": {"tool": "build"}, "effective_action": "compile"}
    absent = toolchain_state_fingerprint(_RegistryOrch(None))

    assert absent is None
    assert compute_retry_key(contract, "x", toolchain_state=absent) == \
        compute_retry_key(contract, "x")
