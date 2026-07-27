# tests/test_repair_contracts.py
"""Plan 6 Stage C Task C3 — typed targeted retrieval and reactive repair.

Spec §C6: retrieval begins only AFTER a current evidence assessment emits a
typed error or capability code, and it reads a bounded, typed selection of the
document map rather than the repository. What comes back is claims or nothing;
`unknown` is a real answer and it is the answer whenever no safe applicable
claim supports a repair.

Four properties are asserted here rather than assumed:

* the typed code — not a failure-signature string — routes the selection, and
  the selection is bounded, so exactly one `cat` per selected entry runs;
* no claims, no proposal (spec hard rule): an empty selection, an unreadable
  entry set and a selection that extracts nothing all return the same empty
  result, and `supporting_claim_ids` is mandatory and non-empty on every
  `RepairContract` that does exist;
* the proposal is ONE public call chosen deterministically from a stored
  claim, never from the model's own reading of a document;
* the engine surfaces the proposal REACTIVELY — one bounded block appended to
  the observation the failing receipt produced — and acceptance is detected by
  exact equality with the proposed call, never self-attested.

Claims, assessments and the document map are consumed through their persisted
shapes only (hand-written fixtures; lanes a1/a2/z1 own the producers).

Scripted-orchestrator style (house pattern, shared with
tests/test_claim_records.py and tests/test_invocation_contracts.py).
"""

import hashlib
import json
import shlex

import pytest
from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)

from sag.agent.claim_records import CLAIM_DIR
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_contracts import (
    DEFAULT_INTENT_SOURCE,
    action_context,
    build_contract,
    contract_hash,
)
from sag.agent.react_types import ReActStep, StepType
from sag.agent.repair_contracts import (
    ACCEPTED_REPAIR_INTENT,
    MAX_RETRIEVED_ENTRIES,
    NO_SAFE_PROPOSAL,
    REPAIR_DIR,
    REPAIR_HEREDOC,
    REPAIR_SCHEMA_VERSION,
    accepted_repair_for,
    build_repair,
    clear_accepted_repair,
    current_acceptance,
    intent_source_for_dispatch,
    is_failure_class,
    propose_public_call,
    repair_block,
    repair_identity,
    retrieve_for,
    select_entries,
    stamp_repair,
    surfacing_block,
    write_repair,
)
from sag.tools.base import ToolResult

CHECKOUT = "/workspace/proj"
DOMAIN = "/workspace/proj/core"
TARGET_SHA = "9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213"


# ---------------------------------------------------------------------------
# hand-written persisted fixtures
# ---------------------------------------------------------------------------


def entry(path, kind, *, sections=(), status="indexed"):
    """A persisted `DocumentMapEntry` (plan §Stage A shared contract)."""
    return {
        "entry_id": "doc-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:12],
        "target_sha": TARGET_SHA,
        "path": path,
        "realpath": path,
        "source_hash": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "kind": kind,
        "section_index": [dict(section) for section in sections],
        "parser_version": "1",
        "discovery_status": status,
    }


def heading(title, start=1, end=9):
    return {
        "section_id": f"sec-{start:04d}",
        "kind": "heading",
        "title_or_key": title,
        "start_line": start,
        "end_line": end,
    }


README = entry(f"{CHECKOUT}/README.md", "markdown", sections=(heading("Building from source"),))
MODULE_DOC = entry(f"{DOMAIN}/README.md", "markdown", sections=(heading("Build"),))
CHANGELOG = entry(f"{CHECKOUT}/CHANGELOG.md", "markdown", sections=(heading("Release notes"),))
WORKFLOW = entry(f"{CHECKOUT}/.github/workflows/ci.yml", "yaml")
CMAKE = entry(f"{CHECKOUT}/CMakeLists.txt", "cmake")
REQUIREMENTS = entry(f"{CHECKOUT}/requirements.txt", "requirements")
PYPROJECT = entry(f"{CHECKOUT}/pyproject.toml", "toml")
DOCKERFILE = entry(f"{CHECKOUT}/Dockerfile", "dockerfile")
POM = entry(f"{DOMAIN}/pom.xml", "xml")

TEXT = {
    README["path"]: "# Building from source\n\n```bash\nmvn -B clean install\n```\n",
    MODULE_DOC["path"]: "# Build\n\n```bash\nmvn -B test\n```\n",
    CHANGELOG["path"]: "# Release notes\n\nNothing to build here.\n",
    WORKFLOW["path"]: "jobs:\n  ci:\n    steps:\n      - run: cmake -DUSE_LLVM=ON ..\n",
    CMAKE["path"]: 'set(USE_LLVM ON)\noption(USE_CUDA "cuda" OFF)\n',
    REQUIREMENTS["path"]: "numpy==1.26.4\n",
    PYPROJECT["path"]: "[project]\nname = 'demo'\n",
    DOCKERFILE["path"]: "FROM base\nRUN pip install numpy==1.26.4\n",
    POM["path"]: "<project/>\n",
}

MAP = {
    "entries": [
        CHANGELOG,
        CMAKE,
        DOCKERFILE,
        MODULE_DOC,
        POM,
        PYPROJECT,
        README,
        REQUIREMENTS,
        WORKFLOW,
    ],
    "document_map_fingerprint": "map-1",
    "partial_map": [],
}


class TextFetcher:
    """The caller's bounded `cat`: one read per selected entry, recorded."""

    def __init__(self, text=None, missing=()):
        self.text = dict(text if text is not None else TEXT)
        self.missing = set(missing)
        self.reads = []

    def __call__(self, selected):
        path = str(selected.get("path") or "")
        self.reads.append(path)
        if path in self.missing:
            return None
        return self.text.get(path)


def ok(output=""):
    return {"success": True, "output": output}


def fail(output=""):
    return {"success": False, "output": output}


class ContainerFS:
    """Execute double with a file layer, so atomic writes are observable.

    Same two shapes the evidence writers use (a single-path `cat` read and the
    `mkdir -p … && cat > tmp <<HEREDOC && mv -f tmp final` write), plus the
    glob read the engine's directory scans issue.
    """

    def __init__(self, files=None, writable=True):
        self.files = dict(files or {})
        self.writable = writable
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat ") and "\n" not in command:
            target = shlex.split(command)[1]
            if target.endswith("/*.json"):
                prefix = target[: -len("*.json")]
                bodies = [
                    body
                    for path, body in sorted(self.files.items())
                    if path.startswith(prefix) and path.endswith(".json")
                ]
                return ok("\n".join(bodies))
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


class ScriptedOrchestrator:
    """`execute_command` over a `ContainerFS` (the engine's read surface)."""

    def __init__(self, files=None):
        self.filesystem = ContainerFS(files=files)

    def execute_command(self, command, **kwargs):
        return self.filesystem(command)


def assessment(typed_code, *, receipt_id="rcpt-maven-0001", fingerprints=None):
    """A persisted `ReceiptAssessment` body (lane z1's shape)."""
    body = {
        "schema_version": 1,
        "assessment_id": f"asm-{receipt_id}-{typed_code}-0000abcd",
        "receipt_id": receipt_id,
        "typed_code": typed_code,
    }
    if fingerprints:
        body["fingerprints"] = dict(fingerprints)
    return body


def lifecycle_claim(argv, *, domain=DOMAIN, claim_id="lifecycle-aaaabbbbcccc", tool="maven"):
    """A persisted lifecycle `PolicyClaim` body (lane a2's shape)."""
    body = {
        "schema_version": 1,
        "claim_id": claim_id,
        "kind": "lifecycle",
        "typed_value": {"tool": tool, "argv": list(argv), "cwd": domain or CHECKOUT},
        "source_class": "repository_doc",
        "source_ref": {
            "entry_id": MODULE_DOC["entry_id"],
            "source_hash": MODULE_DOC["source_hash"],
            "source_range": "L3-L5",
        },
        "source_status": "current",
        "evidence_status": "untested",
        "extraction_method": "markdown_fenced_command",
    }
    if domain:
        body["applicability"] = {"domain": domain}
    return body


def dependency_claim(package, version, *, claim_id="dependency-ddddeeeeffff", domain=DOMAIN):
    body = {
        "schema_version": 1,
        "claim_id": claim_id,
        "kind": "dependency",
        "typed_value": {
            "ecosystem": "pip",
            "package": package,
            "specifier": "==",
            "version": version,
        },
        "source_class": "config",
        "source_ref": {
            "entry_id": REQUIREMENTS["entry_id"],
            "source_hash": REQUIREMENTS["source_hash"],
            "source_range": "L1",
        },
        "source_status": "current",
        "evidence_status": "untested",
        "extraction_method": "requirements_line",
    }
    if domain:
        body["applicability"] = {"domain": domain}
    return body


@pytest.fixture(autouse=True)
def _clean_acceptance_scope():
    clear_accepted_repair()
    yield
    clear_accepted_repair()


# ---------------------------------------------------------------------------
# retrieval: the typed code routes the selection
# ---------------------------------------------------------------------------


def test_capability_code_selects_ci_cmake_and_install_docs_only():
    """Spec §C6 step 1: `capability_absent_*` is answered by the sources that
    can state how a capability is turned on — CI workflows, CMake and the
    install/build sections of documentation. A changelog states none."""
    selected = select_entries("capability_absent_llvm", document_map=MAP, domain_root=DOMAIN)

    kinds = {item["kind"] for item in selected}
    paths = {item["path"] for item in selected}
    assert kinds == {"yaml", "cmake", "markdown"}
    assert WORKFLOW["path"] in paths
    assert CMAKE["path"] in paths
    assert README["path"] in paths
    assert CHANGELOG["path"] not in paths
    assert REQUIREMENTS["path"] not in paths


def test_dependency_code_selects_metadata_requirements_and_docker_only():
    selected = select_entries("dependency_unresolved_numpy", document_map=MAP, domain_root=DOMAIN)

    paths = {item["path"] for item in selected}
    assert paths == {REQUIREMENTS["path"], PYPROJECT["path"], DOCKERFILE["path"]}


def test_other_codes_select_the_domain_s_own_module_docs():
    """Everything else is a question about THIS domain, so it reads this
    domain's documents — not the repository root's, and not a workflow."""
    selected = select_entries("precondition_unmet", document_map=MAP, domain_root=DOMAIN)

    assert [item["path"] for item in selected] == [MODULE_DOC["path"]]


def test_selection_is_bounded_to_five_entries():
    crowded = {
        "entries": [
            entry(f"{CHECKOUT}/docs/g{index}.md", "markdown", sections=(heading("Install"),))
            for index in range(12)
        ]
    }

    selected = select_entries("capability_absent_llvm", document_map=crowded)

    assert MAX_RETRIEVED_ENTRIES == 5
    assert len(selected) == 5
    # Deterministic: the same map always yields the same five, in path order.
    assert [item["path"] for item in selected] == [
        item["path"] for item in select_entries("capability_absent_llvm", document_map=crowded)
    ]


def test_retrieval_reads_each_selected_entry_exactly_once():
    """Spec §C6 step 2: only the selected entries are read, one bounded fetch
    each — retrieval never re-reads the repository."""
    fetcher = TextFetcher()

    result = retrieve_for(
        "capability_absent_llvm",
        document_map=MAP,
        fetch_text=fetcher,
        checkout_root=CHECKOUT,
        applicability={"domain": DOMAIN},
        domain_roots=(DOMAIN,),
    )

    assert len(fetcher.reads) == len(result["entries"]) <= MAX_RETRIEVED_ENTRIES
    assert len(set(fetcher.reads)) == len(fetcher.reads)
    assert set(fetcher.reads) <= set(TEXT)


def test_retrieval_records_claims_and_conflicts_from_the_selected_entries():
    fetcher = TextFetcher()
    filesystem = ContainerFS()

    result = retrieve_for(
        "dependency_unresolved_numpy",
        document_map=MAP,
        fetch_text=fetcher,
        checkout_root=CHECKOUT,
        execute=filesystem,
        domain_roots=(CHECKOUT,),
    )

    kinds = {claim["kind"] for claim in result["claims"]}
    assert "dependency" in kinds
    assert all("claim_id" in claim for claim in result["claims"])
    # Every new claim reached disk under its own id (spec §C6 step 4).
    written = [path for path in filesystem.files if path.startswith(CLAIM_DIR)]
    assert len(written) == len(result["claims"])


def test_equal_applicability_disagreement_is_recorded_as_a_conflict():
    """Both readings are kept; the harness never picks the one that would make
    a repair convenient (spec §C1)."""
    disagreeing = entry(f"{CHECKOUT}/requirements.txt", "requirements")
    fetcher = TextFetcher(text={disagreeing["path"]: "numpy==1.26.4\nnumpy==2.0.0\n"})

    result = retrieve_for(
        "dependency_unresolved_numpy",
        document_map={"entries": [disagreeing]},
        fetch_text=fetcher,
        checkout_root=CHECKOUT,
        domain_roots=(CHECKOUT,),
    )

    assert result["conflicts"]
    assert all(record["kind"] == "claim_conflict" for record in result["conflicts"])
    assert len(result["claims"]) == 2


def test_empty_selection_returns_unknown():
    result = retrieve_for(
        "precondition_unmet",
        document_map={"entries": [WORKFLOW]},
        fetch_text=TextFetcher(),
        checkout_root=CHECKOUT,
        applicability={"domain": DOMAIN},
    )

    assert result == {"entries": [], "claims": [], "conflicts": []}


def test_selection_that_extracts_nothing_returns_unknown():
    """A heading is not an extractor: a document the selection reached but
    which states no claim leaves the loop at `unknown`, not at a guess."""
    silent = entry(f"{DOMAIN}/README.md", "markdown", sections=(heading("Build"),))
    fetcher = TextFetcher(text={silent["path"]: "# Build\n\nSee the wiki.\n"})

    result = retrieve_for(
        "precondition_unmet",
        document_map={"entries": [silent]},
        fetch_text=fetcher,
        checkout_root=CHECKOUT,
        applicability={"domain": DOMAIN},
        domain_roots=(DOMAIN,),
    )

    assert fetcher.reads == [silent["path"]]
    assert result == {"entries": [], "claims": [], "conflicts": []}


def test_unreadable_entries_return_unknown_rather_than_a_partial_guess():
    fetcher = TextFetcher(missing={README["path"], MODULE_DOC["path"], CHANGELOG["path"]})

    result = retrieve_for(
        "capability_absent_llvm",
        document_map={"entries": [README]},
        fetch_text=fetcher,
        checkout_root=CHECKOUT,
    )

    assert result == {"entries": [], "claims": [], "conflicts": []}


# ---------------------------------------------------------------------------
# the proposal: one public call, backed by a stored claim
# ---------------------------------------------------------------------------


def test_no_claims_no_proposal():
    """Spec §C6 hard rule, the one this whole stage exists to enforce."""
    assert build_repair(assessment("precondition_unmet"), []) is None
    call, reason = propose_public_call(assessment("precondition_unmet"), [])
    assert call is None
    assert reason


def test_lifecycle_claim_scoped_to_the_domain_proposes_its_verb():
    claims = [lifecycle_claim(["mvn", "-B", "test"])]

    repair = build_repair(assessment("precondition_unmet"), claims, domain_id="dom-1")

    assert repair["proposed_public_call"] == {
        "tool": "build",
        "params": {"action": "test", "working_directory": DOMAIN},
    }
    assert repair["supporting_claim_ids"] == ["lifecycle-aaaabbbbcccc"]
    assert repair["expected_observations"] == ["report_delta"]


def test_dependency_pin_claims_propose_the_deps_verb_with_their_ids():
    claims = [
        dependency_claim("numpy", "1.26.4", claim_id="dependency-111111111111"),
        dependency_claim("scipy", "1.13.0", claim_id="dependency-222222222222"),
    ]

    repair = build_repair(assessment("dependency_unresolved_numpy"), claims)

    assert repair["proposed_public_call"]["params"]["action"] == "deps"
    assert repair["supporting_claim_ids"] == [
        "dependency-111111111111",
        "dependency-222222222222",
    ]
    # `deps` resolves coordinates; it produces no typed observation yet.
    assert "expected_observations" not in repair


def test_capability_code_proposes_nothing_without_a_lifecycle_claim():
    """The capability path is Stage E's. Until then a capability code with
    claims that only describe dependencies proposes NOTHING."""
    claims = [dependency_claim("numpy", "1.26.4")]

    call, reason = propose_public_call(assessment("capability_absent_llvm"), claims)

    assert call is None
    assert reason == NO_SAFE_PROPOSAL
    assert build_repair(assessment("capability_absent_llvm"), claims) is None


def test_capability_code_accepts_a_directly_applicable_lifecycle_claim():
    claims = [lifecycle_claim(["mvn", "-B", "install"])]

    repair = build_repair(assessment("capability_absent_llvm"), claims)

    assert repair["proposed_public_call"]["params"]["action"] == "install"
    assert repair["typed_failure_or_capability"] == "capability_absent_llvm"


def test_a_lifecycle_claim_outside_the_domain_is_not_applicable():
    claims = [lifecycle_claim(["mvn", "-B", "test"], domain=f"{CHECKOUT}/other")]

    call, reason = propose_public_call(assessment("precondition_unmet"), claims, domain_root=DOMAIN)

    assert call is None
    assert reason == NO_SAFE_PROPOSAL


def test_a_lifecycle_claim_with_no_public_verb_is_not_applicable():
    """`cmake ..` configures; it is not one of the public facade's verbs, and
    a proposal the facade cannot express is not a proposal."""
    claims = [lifecycle_claim(["cmake", ".."], tool="cmake")]

    call, reason = propose_public_call(assessment("precondition_unmet"), claims)

    assert call is None
    assert reason == NO_SAFE_PROPOSAL


def test_repair_carries_its_trigger_and_provenance():
    fingerprints = {"target_sha": TARGET_SHA, "document_map_fingerprint": "map-1"}
    trigger = assessment("precondition_unmet", fingerprints=fingerprints)

    repair = build_repair(
        trigger,
        [lifecycle_claim(["mvn", "-B", "test"])],
        domain_id="dom-1",
        fact_epoch=3,
        open_conflicts=[{"kind": "claim_conflict", "claim_ids": ["a", "b"]}],
    )

    assert repair["schema_version"] == REPAIR_SCHEMA_VERSION
    assert repair["repair_id"] == repair_identity(trigger["assessment_id"])
    assert (
        repair["repair_id"]
        == "rep-" + hashlib.sha256(trigger["assessment_id"].encode("utf-8")).hexdigest()[:12]
    )
    assert repair["trigger_assessment_id"] == trigger["assessment_id"]
    assert repair["trigger_receipt_id"] == "rcpt-maven-0001"
    assert repair["fingerprints"] == fingerprints
    assert repair["domain_id"] == "dom-1"
    assert repair["fact_epoch"] == 3
    assert repair["open_conflicts"] == [{"kind": "claim_conflict", "claim_ids": ["a", "b"]}]
    assert repair["required_preconditions"]
    assert repair["permitted_semantic_envelope"]["tool"] == "build"


def test_a_receiptless_control_assessment_states_no_trigger_receipt():
    """Spec §C6: `trigger_receipt_id` is required for a `ReceiptAssessment`
    and ABSENT for a control assessment — never null, never borrowed."""
    control = {
        "schema_version": 1,
        "assessment_id": "asm-ctl-precondition-0000feed",
        "event_or_intent_id": "ctl-build-0001",
        "stage": "precondition",
        "typed_code": "precondition_unmet",
    }

    repair = build_repair(control, [lifecycle_claim(["mvn", "-B", "test"])])

    assert "trigger_receipt_id" not in repair
    assert repair["trigger_assessment_id"] == "asm-ctl-precondition-0000feed"


def test_a_repair_without_supporting_claims_is_never_built():
    """`supporting_claim_ids` is mandatory: a claim body with no id carries no
    provenance, so it cannot support a proposal."""
    anonymous = lifecycle_claim(["mvn", "-B", "test"])
    anonymous.pop("claim_id")

    assert build_repair(assessment("precondition_unmet"), [anonymous]) is None


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_repair_is_persisted_atomically_under_its_own_id():
    filesystem = ContainerFS()
    repair = build_repair(assessment("precondition_unmet"), [lifecycle_claim(["mvn", "test"])])

    assert write_repair(filesystem, repair) is True

    path = f"{REPAIR_DIR}/{repair['repair_id']}.json"
    assert json.loads(filesystem.files[path]) == repair
    write = filesystem.writes()[0]
    assert write.startswith(f"mkdir -p {shlex.quote(REPAIR_DIR)}")
    assert REPAIR_HEREDOC in write
    assert " && mv -f " in write


def test_writing_the_same_repair_twice_writes_once():
    filesystem = ContainerFS()
    repair = build_repair(assessment("precondition_unmet"), [lifecycle_claim(["mvn", "test"])])

    assert write_repair(filesystem, repair) is True
    assert write_repair(filesystem, repair) is True

    assert len(filesystem.writes()) == 1


def test_a_different_body_under_an_existing_repair_id_is_refused():
    filesystem = ContainerFS()
    repair = build_repair(assessment("precondition_unmet"), [lifecycle_claim(["mvn", "test"])])
    write_repair(filesystem, repair)

    conflicting = dict(repair)
    conflicting["proposed_public_call"] = {"tool": "build", "params": {"action": "package"}}

    assert write_repair(filesystem, conflicting) is False
    assert len(filesystem.writes()) == 1


def test_a_failed_write_is_reported_rather_than_raised():
    filesystem = ContainerFS(writable=False)
    repair = build_repair(assessment("precondition_unmet"), [lifecycle_claim(["mvn", "test"])])

    assert write_repair(filesystem, repair) is False


# ---------------------------------------------------------------------------
# engine surfacing
# ---------------------------------------------------------------------------


def _persisted(orchestrator, repair, trigger):
    orchestrator.filesystem.files[f"{ASSESSMENT_DIR}/{trigger['assessment_id']}.json"] = json.dumps(
        trigger, sort_keys=True
    )
    orchestrator.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )


def _live_repair():
    trigger = assessment("precondition_unmet")
    repair = build_repair(trigger, [lifecycle_claim(["mvn", "-B", "test"])], domain_id="dom-1")
    return trigger, repair


def test_failure_class_codes_are_the_only_retrieval_trigger():
    assert is_failure_class("precondition_unmet") is True
    assert is_failure_class("capability_absent_llvm") is True
    assert is_failure_class("falsifier_empty_delta_despite_success") is True
    assert is_failure_class("expectation_met") is False
    assert is_failure_class("") is False


def test_repair_block_is_the_plan_s_exact_format():
    _, repair = _live_repair()

    assert repair_block(repair) == (
        "[repair] precondition_unmet: proposed "
        'build({"action":"test","working_directory":"/workspace/proj/core"}) '
        "— provenance lifecycle-aaaabbbbcccc; accept by calling it, or state why not."
    )


def test_surfacing_needs_both_a_failure_assessment_and_a_live_proposal():
    trigger, repair = _live_repair()

    orphan = ScriptedOrchestrator()
    orphan.filesystem.files[f"{ASSESSMENT_DIR}/{trigger['assessment_id']}.json"] = json.dumps(
        trigger, sort_keys=True
    )
    assert surfacing_block(orphan, "rcpt-maven-0001") is None

    proposal_only = ScriptedOrchestrator()
    proposal_only.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )
    assert surfacing_block(proposal_only, "rcpt-maven-0001") is None

    both = ScriptedOrchestrator()
    _persisted(both, repair, trigger)
    assert surfacing_block(both, "rcpt-maven-0001") == repair_block(repair)


def test_a_passing_receipt_surfaces_nothing():
    trigger, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    _persisted(orchestrator, repair, trigger)
    met = assessment("expectation_met")
    orchestrator.filesystem.files[f"{ASSESSMENT_DIR}/{met['assessment_id']}.json"] = json.dumps(
        met, sort_keys=True
    )

    assert surfacing_block(orchestrator, "rcpt-other-0002") is None


def _surfacing_engine(forced_engine_factory, orchestrator, result):
    engine, _ = forced_engine_factory()
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


def test_engine_appends_exactly_one_repair_block_to_the_observation(forced_engine):  # noqa: F811
    trigger, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    _persisted(orchestrator, repair, trigger)
    result = ToolResult.completed_failure(
        output="BUILD FAILURE",
        error_code="BUILD_FAILED",
        metadata={"receipt_id": "rcpt-maven-0001"},
    )
    engine = _surfacing_engine(forced_engine, orchestrator, result)

    step = engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    block = repair_block(repair)
    assert step.content.startswith("BUILD FAILURE")
    assert step.content.count("[repair]") == 1
    assert step.content.endswith(block)


def test_engine_surfaces_nothing_when_the_result_names_no_receipt(forced_engine):  # noqa: F811
    trigger, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    _persisted(orchestrator, repair, trigger)
    result = ToolResult.completed_failure(output="refused", error_code="ARGS_INVALID")
    engine = _surfacing_engine(forced_engine, orchestrator, result)

    step = engine._append_native_observation("call-1", "refused", source_tool="build")

    assert step.content == "refused"


def test_engine_surfaces_nothing_for_a_non_build_observation(forced_engine):  # noqa: F811
    trigger, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    _persisted(orchestrator, repair, trigger)
    result = ToolResult.completed_success(output="ok", metadata={"receipt_id": "rcpt-maven-0001"})
    engine = _surfacing_engine(forced_engine, orchestrator, result)

    step = engine._append_native_observation("call-1", "ok", source_tool="project")

    assert step.content == "ok"
    assert orchestrator.filesystem.commands == []


# ---------------------------------------------------------------------------
# acceptance detection
# ---------------------------------------------------------------------------


def test_acceptance_matches_only_the_exact_proposed_call():
    _, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )

    exact = dict(repair["proposed_public_call"]["params"])
    assert accepted_repair_for(orchestrator, "build", exact) == repair["repair_id"]
    assert accepted_repair_for(orchestrator, "build", {**exact, "action": "package"}) is None
    assert accepted_repair_for(orchestrator, "project", exact) is None


def test_engine_detects_acceptance_and_records_it_on_the_frozen_contract(
    forced_engine,  # noqa: F811
):
    """Spec §C6: acceptance creates a NEW intent whose source is the repair —
    the model cannot self-attest provenance, so the id comes from the stored
    proposal the call matched."""
    _, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )
    engine, _ = forced_engine()
    engine.orchestrator = orchestrator

    engine._detect_accepted_repair("build", dict(repair["proposed_public_call"]["params"]))

    assert current_acceptance() == repair["repair_id"]
    assert intent_source_for_dispatch() == ACCEPTED_REPAIR_INTENT

    frozen = build_contract(
        envelope_id="envelope-000001",
        tool="build",
        params=repair["proposed_public_call"]["params"],
        effective_action="test",
        expected_cwd=DOMAIN,
        expected_argv="mvn -B test",
    )
    stamped = stamp_repair(frozen)
    assert stamped["intent_source"] == ACCEPTED_REPAIR_INTENT
    assert stamped["repair_id"] == repair["repair_id"]
    assert stamped["contract_hash"] == contract_hash(stamped)
    assert stamped["contract_id"] == frozen["contract_id"]


def test_a_call_that_matches_no_proposal_clears_the_scope(forced_engine):  # noqa: F811
    _, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )
    engine, _ = forced_engine()
    engine.orchestrator = orchestrator

    engine._detect_accepted_repair("build", dict(repair["proposed_public_call"]["params"]))
    engine._detect_accepted_repair("build", {"action": "package"})

    assert current_acceptance() is None
    assert intent_source_for_dispatch() == DEFAULT_INTENT_SOURCE
    frozen = build_contract(
        envelope_id="envelope-000002",
        tool="build",
        params={"action": "package"},
        effective_action="package",
        expected_cwd=DOMAIN,
        expected_argv="mvn -B package",
    )
    assert stamp_repair(frozen) == frozen


def test_a_harness_authored_call_is_never_an_accepted_repair(forced_engine):  # noqa: F811
    """A forced attempt suppresses the envelope, so the repair scope of the
    previous model call must not leak into the controller's own intent."""
    _, repair = _live_repair()
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem.files[f"{REPAIR_DIR}/{repair['repair_id']}.json"] = json.dumps(
        repair, sort_keys=True
    )
    engine, _ = forced_engine()
    engine.orchestrator = orchestrator
    engine._detect_accepted_repair("build", dict(repair["proposed_public_call"]["params"]))

    with action_context(envelope_id="forced-000009", intent_source="controller"):
        assert intent_source_for_dispatch() == "controller"


def test_acceptance_detection_only_probes_for_the_facade_it_can_propose(
    forced_engine,  # noqa: F811
):
    orchestrator = ScriptedOrchestrator()
    engine, _ = forced_engine()
    engine.orchestrator = orchestrator

    engine._detect_accepted_repair("project", {"action": "analyze"})

    assert orchestrator.filesystem.commands == []
    assert current_acceptance() is None


def test_the_dependency_code_subject_filters_a_multi_pin_set():
    """Live p6v-tvm-r6: the docker script pins several packages, and the
    unfiltered single-pin rule proposed a bare `deps` with no args. The
    typed code names its subject — filtering to it is a citation."""
    from sag.agent.repair_contracts import build_repair

    trigger = {
        "assessment_id": "asm-x-dependency_incompatible_numpy-0daebc4b",
        "receipt_id": "inv-python-2-0003",
        "typed_code": "dependency_incompatible_numpy",
        "detail": "ValueError: Could not convert T.float32 to a NumPy dtype",
    }
    claims = [
        {
            "claim_id": "dependency-numpy",
            "kind": "dependency",
            "typed_value": {
                "ecosystem": "pip",
                "package": "numpy",
                "specifier": "==",
                "version": "1.26.*",
            },
            "applicability": {},
        },
        {
            "claim_id": "dependency-other",
            "kind": "dependency",
            "typed_value": {
                "ecosystem": "pip",
                "package": "tensorflow-aarch64",
                "specifier": "==",
                "version": "2.16.1",
            },
            "applicability": {},
        },
    ]

    repair = build_repair(trigger, claims, domain_root="/workspace/tvm")

    call = repair["proposed_public_call"]
    assert call["params"]["action"] == "deps"
    assert call["params"]["args"] == "numpy==1.26.*"
    assert repair["supporting_claim_ids"] == ["dependency-numpy"]
