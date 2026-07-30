# tests/test_domain_edge_execution_law.py
"""Plan 6 Stage B2: a dependency edge is an EXECUTION law, not a data shape.

Spec §C2: a `version_incompatible` consumer "is sealed blocked with the
mismatched coordinates and receives no runner invocation"; an `unverified`
consumer's "build/test dispatch is locked" until the producer resolves. Plan 5
derived those edges honestly and then dispatched the doomed consumers anyway —
bigtop's stale spark / transaction-queue modules each burned a full reactor run
to rediscover a mismatch the manifest had already stated. A graph nothing obeys
is decoration.

Two halves, one law:

* the build facade refuses the invocation BEFORE any backend materializes argv,
  so the refusal is structurally incapable of touching a runner;
* the verifier walks receipt -> contract -> envelope (`contracts.chain`), so a
  session cannot claim a contract it never persisted, and a pre-Stage-B
  recording — which has no contracts at all — asserts nothing.

The edge fixtures use bigtop's real shape (a data-generator producer whose
consumers pin a stale version) because that is the run this law exists for.
"""

import importlib.util
import json
import os

import pytest

from sag.agent.control_events import action_envelope_sha256
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

PRODUCER = "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-data-generator"
CONSUMER = "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark"
AGGREGATOR = "/workspace/bigtop/bigtop-bigpetstore"
BLOCKED_DETAIL = (
    "requires org.apache.bigtop:bigpetstore-data-generator 3.5.0-SNAPSHOT; "
    "producer builds 3.7.0-SNAPSHOT"
)
UNVERIFIED_DETAIL = (
    "requires org.apache.bigtop:bigpetstore-data-generator 3.5.0-SNAPSHOT; "
    "sibling builds an artifact of this name whose group/version are not "
    "literally declared"
)
SEALED_PHRASE = "this consumer is sealed blocked; record the mismatch, do not silently alias"

# The verbs that physically build. `deps` resolves coordinates and probes/env
# verbs inspect — a doomed consumer may still legally ask what it requires.
GATED_VERBS = ("compile", "test", "package", "install")


def edge(status, *, consumer=CONSUMER, producer=PRODUCER, detail=None, edge_id="edge-0001"):
    body = {
        "consumer": consumer,
        "producer": producer,
        "status": status,
        "detail": detail if detail is not None else BLOCKED_DETAIL,
    }
    if edge_id:
        body["edge_id"] = edge_id
    return body


def manifest(*edges, **extra):
    body = dict(extra)
    if edges:
        body["domain_edges"] = list(edges)
    return body


class EdgeOrchestrator:
    """Marker probes, a build_requirements.json, and a recording assessment sink.

    `read_file` is the container's exact-bytes path, so the manifest never rides
    the marker-probe branch; every OTHER command is recorded, which is how the
    "zero runner invocation" assertions stay honest.
    """

    def __init__(self, requirements, markers=("pom.xml",)):
        self.files = {REQUIREMENTS_PATH: json.dumps(requirements)}
        self.markers = set(markers)
        self.commands = []
        self.assessments = []

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if ASSESSMENT_DIR in command:
            if command.startswith("cat "):
                return {"success": False, "output": "", "exit_code": 1}
            self.assessments.append(json.loads(command.split("\n")[1]))
            return {"success": True, "output": "", "exit_code": 0}
        for marker in self.markers:
            if marker in command:
                return {"success": True, "output": "exists", "exit_code": 0}
        return {"success": True, "output": "missing", "exit_code": 0}


class RecordingBackendTool:
    """Stands in for MavenTool: a call here means a runner was dispatched."""

    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult.completed_success(output="BUILD SUCCESS")


def build_tool(requirements, backend=None):
    return BuildTool(EdgeOrchestrator(requirements), maven_tool=backend or RecordingBackendTool())


def run(requirements, *, action="compile", working_directory=CONSUMER):
    backend = RecordingBackendTool()
    orchestrator = EdgeOrchestrator(requirements)
    tool = BuildTool(orchestrator, maven_tool=backend)
    result = tool.execute(action=action, working_directory=working_directory)
    return result, backend, orchestrator


# -- 1. version_incompatible: sealed blocked, no runner ---------------------


def test_blocked_consumer_is_refused_with_the_edge_detail_verbatim():
    result, backend, _ = run(manifest(edge("version_incompatible")))

    assert not result.succeeded
    assert result.error_code == "DOMAIN_EDGE_BLOCKED"
    assert BLOCKED_DETAIL in result.output
    assert SEALED_PHRASE in result.output
    assert backend.calls == [], "a sealed consumer must receive no runner invocation"


def test_blocked_refusal_names_both_endpoints():
    result, _, _ = run(manifest(edge("version_incompatible")))

    assert CONSUMER in result.output
    assert PRODUCER in result.output


def test_blocked_refusal_writes_a_typed_precondition_control_assessment():
    _, _, orchestrator = run(manifest(edge("version_incompatible")))

    assert len(orchestrator.assessments) == 1
    assessment = orchestrator.assessments[0]
    assert assessment["stage"] == "precondition"
    assert assessment["typed_code"] == "domain_edge_blocked"
    assert assessment["event_or_intent_id"]


def test_blocked_consumer_never_probes_a_runner_or_a_toolchain():
    """The refusal precedes the JDK pre-flight, so nothing is provisioned FOR it."""
    _, _, orchestrator = run(
        manifest(edge("version_incompatible"), java_version="8", java_version_source="pom")
    )

    non_control = [
        command
        for command in orchestrator.commands
        if ASSESSMENT_DIR not in command and not command.startswith("test -f ")
    ]
    assert non_control == [], f"pre-dispatch refusal ran {non_control}"


@pytest.mark.parametrize("verb", GATED_VERBS)
def test_every_building_verb_is_refused_on_a_blocked_consumer(verb):
    result, backend, _ = run(manifest(edge("version_incompatible")), action=verb)

    assert result.error_code == "DOMAIN_EDGE_BLOCKED"
    assert backend.calls == []


def test_a_directory_under_the_consumer_root_is_still_that_consumer():
    result, backend, _ = run(
        manifest(edge("version_incompatible")),
        working_directory=f"{CONSUMER}/src",
    )

    assert result.error_code == "DOMAIN_EDGE_BLOCKED"
    assert backend.calls == []


# -- 2. unverified: locked, and it names who must produce first -------------


def test_unverified_consumer_is_refused_naming_the_producer():
    result, backend, orchestrator = run(manifest(edge("unverified", detail=UNVERIFIED_DETAIL)))

    assert not result.succeeded
    assert result.error_code == "DOMAIN_EDGE_UNVERIFIED"
    assert UNVERIFIED_DETAIL in result.output
    assert PRODUCER in result.output
    assert "must produce first" in result.output
    assert backend.calls == []
    assert orchestrator.assessments[0]["typed_code"] == "domain_edge_unverified"
    assert orchestrator.assessments[0]["stage"] == "precondition"


def test_unverified_refusal_does_not_borrow_the_sealed_wording():
    """`unverified` is unknown, not proven wrong — it must not read as blocked."""
    result, _, _ = run(manifest(edge("unverified", detail=UNVERIFIED_DETAIL)))

    assert SEALED_PHRASE not in result.output


def test_a_version_incompatible_edge_outranks_an_unverified_one():
    result, _, _ = run(
        manifest(
            edge("unverified", detail=UNVERIFIED_DETAIL, edge_id="edge-u"),
            edge("version_incompatible", edge_id="edge-v"),
        )
    )

    assert result.error_code == "DOMAIN_EDGE_BLOCKED"


# -- 3. what the law does NOT refuse ----------------------------------------


def test_deps_is_never_refused_on_a_blocked_consumer():
    """Resolution is how a mismatch gets recorded; refusing it hides the fact."""
    result, backend, _ = run(manifest(edge("version_incompatible")), action="deps")

    assert result.succeeded
    assert backend.calls and backend.calls[0]["command"] == "dependency:resolve"


def test_an_aggregator_above_both_roots_is_not_a_consumer():
    result, backend, _ = run(manifest(edge("version_incompatible")), working_directory=AGGREGATOR)

    assert result.succeeded
    assert backend.calls, "an aggregator above the edge is not the blocked consumer"


def test_nearest_root_binds_a_nested_producer_to_the_producer():
    """Building the producer is exactly what the edge asks for — never refused."""
    nested_producer = f"{CONSUMER}/generator"
    result, backend, _ = run(
        manifest(edge("version_incompatible", producer=nested_producer)),
        working_directory=nested_producer,
    )

    assert result.succeeded
    assert backend.calls


def test_a_compatible_edge_refuses_nothing():
    result, backend, _ = run(manifest(edge("compatible", detail="requires g:n 3.7.0-SNAPSHOT")))

    assert result.succeeded
    assert backend.calls


def test_a_manifest_without_domain_edges_refuses_nothing():
    result, backend, _ = run(manifest())

    assert result.succeeded
    assert backend.calls


def test_a_sibling_of_the_blocked_consumer_is_not_refused():
    result, backend, _ = run(
        manifest(edge("version_incompatible")),
        working_directory=f"{AGGREGATOR}/bigpetstore-mapreduce",
    )

    assert result.succeeded
    assert backend.calls


def test_the_producer_root_itself_is_not_refused():
    result, backend, _ = run(manifest(edge("version_incompatible")), working_directory=PRODUCER)

    assert result.succeeded
    assert backend.calls


def test_edges_nested_under_build_recommendation_are_read_too():
    """Same dual read every other recommendation fact gets (attempt_policy)."""
    result, backend, _ = run(
        {"build_recommendation": {"domain_edges": [edge("version_incompatible")]}}
    )

    assert result.error_code == "DOMAIN_EDGE_BLOCKED"
    assert backend.calls == []


def test_a_malformed_edge_list_is_not_a_refusal():
    result, backend, _ = run({"domain_edges": "not-a-list"})

    assert result.succeeded
    assert backend.calls


# -- 4. verifier: contracts.chain -------------------------------------------


def load_verifier_module():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "verify_native_test_policy.py",
    )
    spec = importlib.util.spec_from_file_location("verify_native_test_policy", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract_payload(*, contract_id="ic-abc123def456", envelope_id="env-1", **extra):
    payload = {
        "schema_version": 1,
        "contract_id": contract_id,
        "envelope_id": envelope_id,
        "effective_action": "compile",
        "expected_cwd": CONSUMER,
        "expected_argv": ["mvn", "compile"],
    }
    payload.update(extra)
    return payload


def write_chain_session(
    tmp_path,
    *,
    receipts=(),
    contracts=(),
    envelope_kind="action_envelope",
    envelope_ids=("env-1",),
):
    """A session directory carrying events, receipts and contracts."""
    session = tmp_path / "session"
    control = session / ".setup_agent"
    control.mkdir(parents=True, exist_ok=True)

    lines = []
    for index, envelope_id in enumerate(envelope_ids, 1):
        exact_params = {"action": "compile", "working_directory": CONSUMER}
        payload = {
            "envelope_id": envelope_id,
            "tool_call_id": f"call-{index}",
            "tool": "build",
            "exact_params": exact_params,
        }
        if envelope_kind == "action_envelope":
            payload["envelope_sha256"] = action_envelope_sha256(
                tool_call_id=f"call-{index}",
                tool="build",
                exact_params=exact_params,
            )
        else:
            payload["action_sha256"] = "unused-by-this-assertion"
        lines.append({"kind": envelope_kind, "payload": payload})
    (control / "control_events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    (control / "verdict.json").write_text(json.dumps({"test_stats": {}}), encoding="utf-8")

    receipt_dir = control / "invocation_receipts"
    receipt_dir.mkdir(exist_ok=True)
    for receipt in receipts:
        (receipt_dir / f"{receipt['receipt_id']}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
    if contracts:
        contract_dir = control / "invocation_contracts"
        contract_dir.mkdir(exist_ok=True)
        for contract in contracts:
            (contract_dir / f"{contract['contract_id']}.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
    return session


def hashed(module, payload):
    """The contract as it is persisted: payload plus its own recomputed hash."""
    body = dict(payload)
    body.pop("contract_hash", None)
    body["contract_hash"] = module.contract_hash_of(body)
    return body


def receipt(*, receipt_id="inv-maven-1-0001", contract_id=None, contract_hash=None):
    body = {
        "schema_version": 2,
        "receipt_id": receipt_id,
        "tool": "maven",
        "effective_action": "compile",
        "working_directory": CONSUMER,
        "exit_code": 0,
        "outcome": "success",
    }
    if contract_id:
        body["contract_id"] = contract_id
    if contract_hash:
        body["contract_hash"] = contract_hash
    return body


def run_chain(session):
    module = load_verifier_module()
    verifier = module.Verifier(str(session))
    verifier.assert_contract_chain()
    return verifier


def chain_failures(verifier):
    return [f for f in verifier.failures if f.split(":")[0] == "contracts.chain"]


def test_contract_hash_formula_is_a_stable_sha256_over_the_payload():
    module = load_verifier_module()
    payload = contract_payload()

    digest = module.contract_hash_of(payload)

    assert len(digest) == 64 and int(digest, 16) >= 0
    assert digest == module.contract_hash_of(dict(payload))
    assert digest != module.contract_hash_of(contract_payload(envelope_id="env-2"))


def test_a_valid_chain_passes(tmp_path):
    module = load_verifier_module()
    contract = hashed(module, contract_payload())
    session = write_chain_session(
        tmp_path,
        receipts=[
            receipt(
                contract_id=contract["contract_id"],
                contract_hash=contract["contract_hash"],
            )
        ],
        contracts=[contract],
    )

    verifier = run_chain(session)

    assert "contracts.chain" in verifier.passes
    assert verifier.failures == []


def test_a_forced_action_envelope_satisfies_the_chain(tmp_path):
    module = load_verifier_module()
    contract = hashed(module, contract_payload())
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id=contract["contract_id"])],
        contracts=[contract],
        envelope_kind="forced_action",
    )

    verifier = run_chain(session)

    assert "contracts.chain" in verifier.passes


def test_a_receipt_whose_contract_file_is_missing_fails(tmp_path):
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id="ic-abc123def456")],
        contracts=[],
    )

    verifier = run_chain(session)

    assert chain_failures(verifier)
    assert "ic-abc123def456" in chain_failures(verifier)[0]


def test_a_contract_whose_hash_does_not_recompute_fails(tmp_path):
    module = load_verifier_module()
    contract = hashed(module, contract_payload())
    contract["contract_hash"] = "0" * 64
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id=contract["contract_id"])],
        contracts=[contract],
    )

    verifier = run_chain(session)

    assert chain_failures(verifier)


def test_a_contract_edited_after_the_fact_fails(tmp_path):
    """The hash is the seal: rewriting a field must break the chain."""
    module = load_verifier_module()
    contract = hashed(module, contract_payload())
    contract["expected_argv"] = ["mvn", "compile", "-DskipTests"]
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id=contract["contract_id"])],
        contracts=[contract],
    )

    verifier = run_chain(session)

    assert chain_failures(verifier)


def test_a_contract_naming_an_envelope_the_session_never_emitted_fails(tmp_path):
    module = load_verifier_module()
    contract = hashed(module, contract_payload(envelope_id="env-never"))
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id=contract["contract_id"])],
        contracts=[contract],
    )

    verifier = run_chain(session)

    assert chain_failures(verifier)
    assert "env-never" in chain_failures(verifier)[0]


def test_a_receipt_disagreeing_with_its_contract_hash_fails(tmp_path):
    module = load_verifier_module()
    contract = hashed(module, contract_payload())
    session = write_chain_session(
        tmp_path,
        receipts=[receipt(contract_id=contract["contract_id"], contract_hash="f" * 64)],
        contracts=[contract],
    )

    verifier = run_chain(session)

    assert chain_failures(verifier)


def test_a_pre_stage_b_session_is_silent(tmp_path):
    """Plan 5 receipts carry no contract_id — the assertion states nothing."""
    session = write_chain_session(tmp_path, receipts=[receipt()], contracts=[])

    verifier = run_chain(session)

    assert "contracts.chain" not in verifier.passes
    assert verifier.failures == []


def test_a_session_with_no_receipts_at_all_is_silent(tmp_path):
    session = write_chain_session(tmp_path)

    verifier = run_chain(session)

    assert verifier.passes == []
    assert verifier.failures == []


LIVE_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)
LIVE_BIGTOP = os.path.join(LIVE_LOGS, "session_20260726_195220_99607")


@pytest.mark.skipif(not os.path.isdir(LIVE_BIGTOP), reason="recorded bigtop session not present")
def test_the_recorded_bigtop_session_keeps_its_assertion_set():
    """Plan 5 recordings have receipts and no contracts: silent, still green."""
    module = load_verifier_module()
    verifier = module.Verifier(LIVE_BIGTOP)
    verifier.assert_pairing_and_hashes()
    verifier.assert_receipts_immutable()
    verifier.assert_contract_chain()
    verifier.assert_bigtop()

    assert verifier.failures == []
    assert "contracts.chain" not in verifier.passes
    assert len(verifier.passes) == 10
