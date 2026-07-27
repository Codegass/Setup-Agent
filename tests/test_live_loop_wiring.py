# tests/test_live_loop_wiring.py
"""Plan 6 Stage F Task F1 — the contract loop, wired into a live run.

Stages A–E built the machinery and proved it in isolation. The verified gap
this suite closes is that `discover_document_map`, the claim extractors,
`claim_graph` and repair CREATION had zero production callers: every one of
them was exercised only by its own lane's tests, so a real session produced no
map, no claims, no proposals and no claim transitions.

Three seams are asserted here, each against the shape a real run leaves behind:

* the SURVEY seam — analyze discovers the bounded map, extracts the claims its
  indexed entries state, and persists both, so the DomainFacts projection that
  follows reads real claims; either half failing is a recorded conflict on the
  analysis, never an analyze failure;
* the REPAIR seam — a failure-class assessment with no proposal on disk gets
  one built from a bounded, typed retrieval over the persisted map, and the
  observation the failing receipt produced then surfaces it; one creation
  attempt per assessment, whatever the answer was;
* the CLAIM seam — `expectation_met` confirms the claims its contract cited and
  a typed `falsifier_*` contradicts them and retracts what rested on them, as
  ONE event group with a terminal record (plan §Stage C binding note (a)).

Fake-orchestrator style (house pattern, shared with tests/test_document_map.py
and tests/test_repair_contracts.py); the engine plumbing is the shared
`forced_engine` fixture from tests/test_forced_attempt_native.py.
"""

import hashlib
import json
import shlex

from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)

from sag.agent.claim_graph import CLAIM_GRAPH_PATH, group_identity, read_claim_files
from sag.agent.claim_records import CLAIM_DIR, entry_has_extractors
from sag.agent.control_events import ControlEvent
from sag.agent.document_map import DOCUMENT_MAP_PATH, MAX_FILE_BYTES, read_entry_text
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.invocation_contracts import CONTRACT_DIR
from sag.agent.react_types import ReActStep, StepType
from sag.agent.repair_contracts import REPAIR_DIR, read_records, repair_block
from sag.tools.base import ToolResult
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

ROOT = "/workspace/proj"
DOMAIN = f"{ROOT}/core"
SHA = "9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213"


# ---------------------------------------------------------------------------
# the virtual checkout the survey reads
# ---------------------------------------------------------------------------

README = """# Building from source

```bash
mvn -B test
```
"""

WORKFLOW = """jobs:
  build:
    steps:
      - run: export CMAKE_ARGS="-DUSE_LLVM=ON"
"""

CMAKE = 'set(USE_LLVM OFF)\noption(USE_CUDA "cuda" OFF)\n'

REQUIREMENTS = "numpy==1.26.4\n"

# Indexed by the map, read by NO extractor: fetching its text again would be a
# probe with no possible product.
PYPROJECT = "[project]\nname = 'demo'\n"

CHECKOUT = {
    "README.md": README,
    ".github/workflows/ci.yml": WORKFLOW,
    "CMakeLists.txt": CMAKE,
    "requirements.txt": REQUIREMENTS,
    "pyproject.toml": PYPROJECT,
}


def ok(output=""):
    return {"success": True, "output": output, "exit_code": 0}


def fail(output=""):
    return {"success": False, "output": output, "exit_code": 1}


class FakeContainer:
    """One virtual container: a checkout to survey and an evidence directory.

    Only the shapes this wiring issues are modelled — the map's enumeration
    `find`, its batched `realpath` containment probe and its bounded `head -c`
    fetch, the evidence writers' `mkdir -p … && cat > tmp <<HEREDOC && mv -f`,
    the single-path and glob `cat` reads, and the sha probe. `files` is the
    persisted layer keyed by absolute path, which is also the test-double API
    `read_container_text` reads the manifest through.
    """

    def __init__(self, checkout=None, files=None, *, root=ROOT, sha=SHA, writable=True):
        self.checkout = dict(checkout or {})
        self.files = dict(files or {})
        self.root = root
        self.sha = sha
        self.writable = writable
        self.commands = []

    # -- helpers ---------------------------------------------------------
    def _relative(self, path):
        prefix = f"{self.root}/"
        return path[len(prefix) :] if path.startswith(prefix) else None

    def _read(self, path):
        if path in self.files:
            return self.files[path]
        relative = self._relative(path)
        if relative is not None and relative in self.checkout:
            return self.checkout[relative]
        return None

    # -- transport -------------------------------------------------------
    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "rev-parse HEAD" in command:
            return ok(self.sha) if self.sha else fail("")
        if " find . " in command:
            return ok("".join(f"./{name}\n" for name in sorted(self.checkout)))
        if command.startswith("realpath "):
            arguments = shlex.split(command)
            paths = arguments[arguments.index("--") + 1 :]
            return ok("".join(f"{path}\n" for path in paths))
        if command.startswith("head -c "):
            arguments = shlex.split(command)
            path = arguments[arguments.index("--") + 1]
            body = self._read(path)
            if body is None:
                return fail(f"head: {path}: No such file or directory")
            return ok(body[: int(arguments[2])])
        if "mv -f " in command and "\n" in command:
            if not self.writable:
                return fail("Read-only file system")
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            self.files[header.rsplit("mv -f ", 1)[1].split()[1]] = body
            return ok("")
        if "<<'SAGEOF'" in command:  # the manifest's own heredoc write
            path = command.split("cat > ", 1)[1].split(" ", 1)[0]
            self.files[path] = command.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            return ok("")
        if command.startswith("cat "):
            target = shlex.split(command.replace(" 2>/dev/null", ""))[-1]
            if target.endswith("/*.json"):
                prefix = target[: -len("*.json")]
                return ok(
                    "\n".join(
                        body
                        for path, body in sorted(self.files.items())
                        if path.startswith(prefix) and path.endswith(".json")
                    )
                )
            body = self._read(target)
            return ok(body) if body is not None else fail("No such file or directory")
        return ok("")

    def execute_command(self, command, **kwargs):
        return self(command, **kwargs)

    # -- assertions ------------------------------------------------------
    def fetches(self):
        """The paths whose text was fetched, in order (discovery + extraction)."""
        paths = []
        for command in self.commands:
            if command.startswith("head -c "):
                arguments = shlex.split(command)
                paths.append(arguments[arguments.index("--") + 1])
        return paths

    def claims(self):
        return {
            path: json.loads(body)
            for path, body in self.files.items()
            if path.startswith(f"{CLAIM_DIR}/")
        }


def analyzer(container):
    return ProjectAnalyzerTool(docker_orchestrator=container)


def conflict_kinds(analysis):
    return [record["kind"] for record in analysis.get("survey_conflicts") or ()]


def claims_by_kind(container):
    grouped = {}
    for body in container.claims().values():
        grouped.setdefault(body["kind"], []).append(body)
    return grouped


# ---------------------------------------------------------------------------
# item 1: the survey seam — analyze produces the map and the claims
# ---------------------------------------------------------------------------


def test_analyze_persists_the_document_map_it_discovered():
    container = FakeContainer(CHECKOUT)
    analysis = {}

    document_map = analyzer(container)._survey_documents_and_claims(ROOT, analysis)

    persisted = json.loads(container.files[DOCUMENT_MAP_PATH])
    assert [entry["path"] for entry in persisted["entries"]] == [
        f"{ROOT}/.github/workflows/ci.yml",
        f"{ROOT}/CMakeLists.txt",
        f"{ROOT}/README.md",
        f"{ROOT}/pyproject.toml",
        f"{ROOT}/requirements.txt",
    ]
    assert persisted["document_map_fingerprint"] == document_map["document_map_fingerprint"]
    assert analysis["document_map_fingerprint"] == persisted["document_map_fingerprint"]


def test_analyze_persists_the_claims_the_indexed_entries_state():
    """Spec §C1: the map says what exists, the claims say what it STATES."""
    container = FakeContainer(CHECKOUT)

    analyzer(container)._survey_documents_and_claims(ROOT, {})

    by_kind = claims_by_kind(container)
    lifecycle = [body for body in by_kind["lifecycle"] if body["typed_value"]["tool"] == "maven"]
    assert lifecycle[0]["typed_value"]["argv"] == ["mvn", "-B", "test"]
    assert lifecycle[0]["source_class"] == "repository_doc"
    assert {"USE_LLVM", "USE_CUDA", "CMAKE_ARGS"} <= {
        body["typed_value"]["name"] for body in by_kind["env"]
    }
    assert by_kind["dependency"][0]["typed_value"] == {
        "ecosystem": "pip",
        "package": "numpy",
        "specifier": "==",
        "version": "1.26.4",
    }


def test_only_entries_whose_kind_has_an_extractor_are_fetched_again():
    """The map already hashed every entry; a second fetch is only warranted
    where an extractor can turn the text into a claim."""
    container = FakeContainer(CHECKOUT)

    analyzer(container)._survey_documents_and_claims(ROOT, {})

    fetched = container.fetches()
    assert fetched.count(f"{ROOT}/pyproject.toml") == 1  # discovery only
    assert fetched.count(f"{ROOT}/README.md") == 2  # discovery + extraction


def test_entry_has_extractors_names_exactly_the_kinds_the_extractors_read():
    assert entry_has_extractors({"kind": "markdown", "path": f"{ROOT}/README.md"})
    assert entry_has_extractors({"kind": "yaml", "path": f"{ROOT}/ci.yml"})
    assert entry_has_extractors({"kind": "cmake", "path": f"{ROOT}/CMakeLists.txt"})
    assert entry_has_extractors({"kind": "requirements", "path": f"{ROOT}/requirements.txt"})
    assert entry_has_extractors({"kind": "dockerfile", "path": f"{ROOT}/Dockerfile"})
    assert entry_has_extractors({"kind": "xml", "path": f"{ROOT}/pom.xml"})
    assert not entry_has_extractors({"kind": "toml", "path": f"{ROOT}/pyproject.toml"})
    assert not entry_has_extractors({"kind": "gradle", "path": f"{ROOT}/build.gradle"})


def test_entry_text_is_fetched_under_the_map_s_own_byte_budget():
    container = FakeContainer(CHECKOUT)

    text = read_entry_text(container.execute_command, {"path": f"{ROOT}/README.md"})

    assert text == README
    assert f"head -c {MAX_FILE_BYTES} -- " in container.commands[-1]


def test_a_failed_discovery_records_a_named_conflict_and_analyze_continues():
    """Plan §F1 item 1: failure of either half is a recorded conflict, never an
    analyze failure."""
    container = FakeContainer(CHECKOUT, writable=False)
    analysis = {}

    assert analyzer(container)._survey_documents_and_claims(ROOT, analysis) is None

    assert conflict_kinds(analysis) == ["document_map_failed"]
    assert "document_map_fingerprint" not in analysis


def test_a_failed_claim_write_records_claim_extraction_failed():
    class RefusesClaims(FakeContainer):
        def __call__(self, command, **kwargs):
            if CLAIM_DIR in command and "mv -f " in command:
                self.commands.append(command)
                return fail("Read-only file system")
            return super().__call__(command, **kwargs)

    container = RefusesClaims(CHECKOUT)
    analysis = {}

    analyzer(container)._survey_documents_and_claims(ROOT, analysis)

    assert conflict_kinds(analysis) == ["claim_extraction_failed"]
    # The map still landed: the two halves fail independently.
    assert DOCUMENT_MAP_PATH in container.files


def test_a_survey_without_a_transport_states_nothing_and_raises_nothing():
    analysis = {}

    assert ProjectAnalyzerTool()._survey_documents_and_claims(ROOT, analysis) is None

    assert "survey_conflicts" not in analysis


def test_the_document_survey_runs_before_the_manifest_is_persisted(monkeypatch):
    """The DomainFacts projection reads the claims, so the claims must already
    be on disk when the manifest is written."""
    tool = analyzer(FakeContainer(CHECKOUT))
    order = []

    monkeypatch.setattr(tool, "_analyze_project_structure", lambda path: {"project_type": "Python"})
    monkeypatch.setattr(tool, "_analyze_documentation", lambda path: {})
    monkeypatch.setattr(tool, "_analyze_build_configuration", lambda path, kind: {})
    monkeypatch.setattr(tool, "_analyze_test_configuration", lambda path, kind: {})
    monkeypatch.setattr(tool, "_recommend_build_approach", lambda path, analysis: {})
    monkeypatch.setattr(tool, "_recommend_test_approach", lambda path, rec: None)
    monkeypatch.setattr(
        tool,
        "_survey_documents_and_claims",
        lambda path, analysis: order.append("documents") or {"entries": []},
    )
    monkeypatch.setattr(
        tool,
        "_persist_build_requirements",
        lambda path, analysis, **kwargs: order.append("manifest"),
    )

    tool._perform_comprehensive_analysis(ROOT)

    assert order == ["documents", "manifest"]


def test_the_manifest_survey_stamp_carries_the_document_map_fingerprint():
    container = FakeContainer(CHECKOUT)
    tool = analyzer(container)
    analysis = {"build_recommendation": {}}

    document_map = tool._survey_documents_and_claims(ROOT, analysis)
    tool._persist_build_requirements(ROOT, analysis, document_map=document_map)

    survey = json.loads(container.files[REQUIREMENTS_PATH])["survey"]
    assert survey["document_map_fingerprint"] == analysis["document_map_fingerprint"]


def test_a_survey_without_a_map_states_no_document_map_fingerprint():
    """Absent facts are absent keys — a manifest with no map states none."""
    container = FakeContainer(CHECKOUT)

    analyzer(container)._persist_build_requirements(ROOT, {"build_recommendation": {}})

    survey = json.loads(container.files[REQUIREMENTS_PATH])["survey"]
    assert "document_map_fingerprint" not in survey


# ---------------------------------------------------------------------------
# hand-written persisted fixtures for the engine seams
# ---------------------------------------------------------------------------

RECEIPT_ID = "rcpt-maven-0001"
CONTRACT_ID = "ic-000001-abcdefabcdef"
MODULE_README = "# Build\n\n```bash\nmvn -B test\n```\n"


def entry_body(path, kind, *, sections=()):
    return {
        "entry_id": "doc-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:12],
        "target_sha": SHA,
        "path": path,
        "realpath": path,
        "source_hash": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "kind": kind,
        "section_index": [dict(section) for section in sections],
        "parser_version": "1",
        "discovery_status": "indexed",
    }


MODULE_DOC = entry_body(
    f"{DOMAIN}/README.md",
    "markdown",
    sections=(
        {
            "section_id": "sec-0001",
            "kind": "heading",
            "title_or_key": "Build",
            "start_line": 1,
            "end_line": 5,
        },
    ),
)

DOCUMENT_MAP_BODY = {
    "schema_version": 1,
    "parser_version": "1",
    "document_map_fingerprint": "map-fingerprint-1",
    "entries": [MODULE_DOC],
    "partial_map": [],
}


def assessment_body(typed_code, *, receipt_id=RECEIPT_ID, suffix="0000abcd"):
    return {
        "schema_version": 1,
        "assessment_id": f"asm-{receipt_id}-{typed_code}-{suffix}",
        "receipt_id": receipt_id,
        "typed_code": typed_code,
    }


def claim_body(claim_id, kind, typed_value, *, support=()):
    body = {
        "schema_version": 1,
        "claim_id": claim_id,
        "kind": kind,
        "typed_value": dict(typed_value),
        "source_class": "repository_doc",
        "source_ref": {
            "entry_id": MODULE_DOC["entry_id"],
            "source_hash": MODULE_DOC["source_hash"],
            "source_range": "L3",
        },
        "source_status": "current",
        "evidence_status": "untested",
        "extraction_method": "markdown_fenced_command",
        "applicability": {"domain": DOMAIN},
    }
    if support:
        body["support_claim_ids"] = list(support)
    return body


LIFECYCLE_CLAIM = claim_body(
    "lifecycle-aaaabbbbcccc",
    "lifecycle",
    {"tool": "maven", "argv": ["mvn", "-B", "test"], "cwd": DOMAIN},
)
CONCLUSION_CLAIM = claim_body(
    "tool_constraint-ddddeeeeffff",
    "tool_constraint",
    {"tool": "maven", "constraint": "[3.9,)"},
    support=(LIFECYCLE_CLAIM["claim_id"],),
)

MANIFEST = {
    "survey": {
        "project_path": ROOT,
        "config_fingerprint": "cfg-1",
        "document_map_fingerprint": DOCUMENT_MAP_BODY["document_map_fingerprint"],
    },
    "build_root": DOMAIN,
    "build_domains": [{"root": DOMAIN, "system": "maven"}],
    "domain_facts": [{"domain_id": "dom-000001", "root": DOMAIN, "fact_epoch": 1}],
}

CONTRACT = {
    "schema_version": 1,
    "contract_id": CONTRACT_ID,
    "envelope_id": "envelope-000001",
    "domain_id": "dom-000001",
    "fact_epoch": 1,
    "intent_source": "model",
    "requested_call": {"tool": "build", "params": {"action": "test"}},
    "effective_action": "test",
    "expected_cwd": DOMAIN,
    "expected_argv": "mvn -B test",
    "supporting_claim_ids": [LIFECYCLE_CLAIM["claim_id"]],
}


def engine_container(*, assessments=(), claims=(), repairs=(), contract=CONTRACT):
    files = {
        REQUIREMENTS_PATH: json.dumps(MANIFEST, sort_keys=True),
        DOCUMENT_MAP_PATH: json.dumps(DOCUMENT_MAP_BODY, sort_keys=True),
    }
    if contract:
        files[f"{CONTRACT_DIR}/{contract['contract_id']}.json"] = json.dumps(
            contract, sort_keys=True
        )
    for body in assessments:
        files[f"{ASSESSMENT_DIR}/{body['assessment_id']}.json"] = json.dumps(body, sort_keys=True)
    for body in claims:
        files[f"{CLAIM_DIR}/{body['claim_id']}.json"] = json.dumps(body, sort_keys=True)
    for body in repairs:
        files[f"{REPAIR_DIR}/{body['repair_id']}.json"] = json.dumps(body, sort_keys=True)
    return FakeContainer({"core/README.md": MODULE_README}, files)


def wire(forced_engine_factory, container, *, metadata):
    engine, _ = forced_engine_factory()
    engine.orchestrator = container
    engine.steps = [
        ReActStep(
            step_type=StepType.ACTION,
            content="build",
            tool_name="build",
            tool_params={"action": "test"},
            tool_result=ToolResult.completed_failure(
                output="BUILD FAILURE",
                error_code="BUILD_FAILED",
                metadata=dict(metadata),
            ),
            timestamp="2026-07-26T00:00:00Z",
            tool_call_id="call-1",
        )
    ]
    return engine


def transitions(engine):
    return [payload for kind, payload in engine.control_events if kind == "claim_transition"]


def repairs_on_disk(container):
    return [
        json.loads(body)
        for path, body in sorted(container.files.items())
        if path.startswith(f"{REPAIR_DIR}/")
    ]


# ---------------------------------------------------------------------------
# item 2: the repair seam — a fresh failure assessment gets its proposal
# ---------------------------------------------------------------------------


def test_a_failure_assessment_without_a_repair_gets_one_and_it_is_surfaced(
    forced_engine,  # noqa: F811
):
    """Spec §C6: retrieval is reactive — this is the first moment it is legal,
    and the block the model sees is the proposal it just produced."""
    trigger = assessment_body("semantic_failure")
    container = engine_container(assessments=[trigger])
    engine = wire(forced_engine, container, metadata={"receipt_id": RECEIPT_ID})

    step = engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    created = repairs_on_disk(container)
    assert len(created) == 1
    repair = created[0]
    assert repair["trigger_assessment_id"] == trigger["assessment_id"]
    assert repair["proposed_public_call"] == {
        "tool": "build",
        "params": {"action": "test", "working_directory": DOMAIN},
    }
    assert step.content.count("[repair]") == 1
    assert step.content.endswith(repair_block(repair))


def test_the_claims_the_proposal_cites_are_persisted_with_it(
    forced_engine,  # noqa: F811
):
    """Provenance must be lookupable: a proposal citing an unstored claim
    would be self-attested."""
    container = engine_container(assessments=[assessment_body("semantic_failure")])
    engine = wire(forced_engine, container, metadata={"receipt_id": RECEIPT_ID})

    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    repair = repairs_on_disk(container)[0]
    stored = {body["claim_id"] for body in container.claims().values()}
    assert repair["supporting_claim_ids"]
    assert set(repair["supporting_claim_ids"]) <= stored


def test_repair_creation_is_attempted_once_per_assessment(
    forced_engine,  # noqa: F811
):
    """Bounded: a second observation about the same failure must not re-read
    the repository, whatever the first answer was."""
    container = engine_container(assessments=[assessment_body("semantic_failure")])
    engine = wire(forced_engine, container, metadata={"receipt_id": RECEIPT_ID})

    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")
    reads = container.fetches()
    engine._append_native_observation("call-2", "BUILD FAILURE", source_tool="build")

    assert reads
    assert container.fetches() == reads


def test_an_assessment_that_already_has_a_repair_reads_no_document(
    forced_engine,  # noqa: F811
):
    trigger = assessment_body("semantic_failure")
    repair = {
        "schema_version": 1,
        "repair_id": "rep-000000000001",
        "trigger_assessment_id": trigger["assessment_id"],
        "typed_failure_or_capability": trigger["typed_code"],
        "proposed_public_call": {
            "tool": "build",
            "params": {"action": "test", "working_directory": DOMAIN},
        },
        "supporting_claim_ids": [LIFECYCLE_CLAIM["claim_id"]],
    }
    container = engine_container(assessments=[trigger], repairs=[repair])
    engine = wire(forced_engine, container, metadata={"receipt_id": RECEIPT_ID})

    step = engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    assert container.fetches() == []
    assert step.content.endswith(repair_block(repair))


def test_a_passing_receipt_creates_no_repair(forced_engine):  # noqa: F811
    container = engine_container(assessments=[assessment_body("expectation_met")])
    engine = wire(forced_engine, container, metadata={"receipt_id": RECEIPT_ID})

    engine._append_native_observation("call-1", "50 tests passed", source_tool="build")

    assert repairs_on_disk(container) == []


def test_repair_creation_never_breaks_the_observation(forced_engine):  # noqa: F811
    class Hostile(FakeContainer):
        def execute_command(self, command, **kwargs):
            raise RuntimeError("the container is gone")

    engine = wire(forced_engine, Hostile({}), metadata={"receipt_id": RECEIPT_ID})

    step = engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    assert step.content == "BUILD FAILURE"


# ---------------------------------------------------------------------------
# item 3: the claim seam — what a verdict does to the claims it rested on
# ---------------------------------------------------------------------------


def test_expectation_met_confirms_the_claims_the_contract_cited(
    forced_engine,  # noqa: F811
):
    trigger = assessment_body("expectation_met")
    container = engine_container(assessments=[trigger], claims=[LIFECYCLE_CLAIM])
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "50 tests passed", source_tool="build")

    group = group_identity(trigger["assessment_id"])
    assert transitions(engine) == [
        {
            "group_id": group,
            "claim_id": LIFECYCLE_CLAIM["claim_id"],
            "from_status": "untested",
            "to_status": "confirmed",
            "cause_assessment_id": trigger["assessment_id"],
        },
        {"group_id": group, "terminal": True},
    ]
    snapshot = json.loads(container.files[CLAIM_GRAPH_PATH])
    assert {claim["claim_id"]: claim.get("evidence_status") for claim in snapshot["claims"]} == {
        LIFECYCLE_CLAIM["claim_id"]: "confirmed"
    }


def test_the_committed_group_validates_as_control_events(forced_engine):  # noqa: F811
    """The engine's emitter swallows emission failures by design, so a payload
    the strict sink refuses would lose the whole group in silence."""
    container = engine_container(
        assessments=[assessment_body("falsifier_report_delta")],
        claims=[LIFECYCLE_CLAIM, CONCLUSION_CLAIM],
    )
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "exit 0, no report", source_tool="build")

    emitted = transitions(engine)
    assert emitted
    for sequence, payload in enumerate(emitted, start=1):
        event = ControlEvent(sequence=sequence, kind="claim_transition", payload=payload)
        assert event.payload == payload


def test_a_falsifier_contradicts_the_claims_and_retracts_what_rested_on_them(
    forced_engine,  # noqa: F811
):
    trigger = assessment_body("falsifier_report_delta")
    container = engine_container(assessments=[trigger], claims=[LIFECYCLE_CLAIM, CONCLUSION_CLAIM])
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "exit 0, no report", source_tool="build")

    group = group_identity(trigger["assessment_id"])
    moved = {
        payload["claim_id"]: payload["to_status"]
        for payload in transitions(engine)
        if not payload.get("terminal")
    }
    assert moved == {
        LIFECYCLE_CLAIM["claim_id"]: "contradicted",
        CONCLUSION_CLAIM["claim_id"]: "unknown",
    }
    assert {payload["group_id"] for payload in transitions(engine)} == {group}
    assert transitions(engine)[-1] == {"group_id": group, "terminal": True}


def test_an_honest_failure_moves_no_claim(forced_engine):  # noqa: F811
    """Spec §C5: a compiler error is a real fact about the run and falsifies
    nothing the documents state."""
    container = engine_container(
        assessments=[assessment_body("expectation_unmet")], claims=[LIFECYCLE_CLAIM]
    )
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "BUILD FAILURE", source_tool="build")

    assert transitions(engine) == []
    assert CLAIM_GRAPH_PATH not in container.files


def test_a_contract_citing_an_unpersisted_claim_is_skipped_rather_than_crashing(
    forced_engine,  # noqa: F811
):
    container = engine_container(assessments=[assessment_body("expectation_met")])
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    step = engine._append_native_observation("call-1", "50 tests passed", source_tool="build")

    assert transitions(engine) == []
    assert step.content == "50 tests passed"


def test_the_graph_and_the_repair_layer_read_the_claims_directory_identically():
    """`claim_graph.read_claim_files` restates the Stage C3 glob read so the C1
    graph does not depend on the C3 layer to read its own subjects. Two readers
    of one directory must not disagree about what a stored claim is."""
    container = engine_container(claims=[LIFECYCLE_CLAIM, CONCLUSION_CLAIM])

    assert read_claim_files(container.execute_command) == sorted(
        read_records(container, CLAIM_DIR), key=lambda body: body["claim_id"]
    )


def test_claim_transitions_are_committed_once_per_assessment(
    forced_engine,  # noqa: F811
):
    container = engine_container(
        assessments=[assessment_body("expectation_met")], claims=[LIFECYCLE_CLAIM]
    )
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "50 tests passed", source_tool="build")
    engine._append_native_observation("call-2", "50 tests passed", source_tool="build")

    assert len(transitions(engine)) == 2  # one move + one terminal record


def test_a_contract_that_cites_no_claim_moves_nothing(forced_engine):  # noqa: F811
    contract = {key: value for key, value in CONTRACT.items() if key != "supporting_claim_ids"}
    container = engine_container(
        assessments=[assessment_body("expectation_met")],
        claims=[LIFECYCLE_CLAIM],
        contract=contract,
    )
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "50 tests passed", source_tool="build")

    assert transitions(engine) == []


def test_a_non_build_observation_reads_no_evidence_at_all(forced_engine):  # noqa: F811
    container = engine_container(
        assessments=[assessment_body("expectation_met")], claims=[LIFECYCLE_CLAIM]
    )
    engine = wire(
        forced_engine,
        container,
        metadata={"receipt_id": RECEIPT_ID, "contract_id": CONTRACT_ID},
    )

    engine._append_native_observation("call-1", "analyzed", source_tool="project")

    assert container.commands == []
