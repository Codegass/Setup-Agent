# tests/test_receipt_proven_structure.py
"""Plan 8 §3.6 — the survey only PROPOSED what polaris is made of.

p7d polaris (`logs/session_20260729_111737_22356`): `settings.gradle.kts`
registers its 26 subprojects imperatively, the survey parsed none of them, and
the manifest went out saying `root_shape: single_module`, `build_islands: []`.
Everything keyed on that map was keyed on a guess — no domains, so the P0-F
no-upgrade cap never armed; no per-module expectation, so coverage had nothing
to measure; no islands, so no checklist coordinates.

The build itself knew. Maven prints a reactor summary and Gradle prints the
tasks it ran, both parsed already, both riding the invocation receipt since
Plan 7 (#17). What was missing is the ladder this project already applies to
every other fact: a receipt-proven statement outranks a survey guess the moment
it exists (spec §2 P3). So a terminal receipt that names its modules persists
the structure at RECEIPT provenance, and from then on the denominator stands on
what real work stated. A newer terminal receipt may restate it. A survey re-run
— with the same blind spot that produced the guess — may never demote it.

Deliberately NOT in scope: parsing Kotlin settings or imperative version checks
statically. Pre-flight owns stated-requirement recovery and owns it well.
"""

import json

from sag.agent.invocation_receipts import build_receipt, record_invocation
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_structure import (
    STRUCTURE_KEY,
    module_key,
    promote_structure,
    read_module_structure,
    structure_from_receipt,
    structure_updates,
)
from sag.runtime.paths import BUILD_REQUIREMENTS_PATH
from sag.tools.internal.build_preflight import read_build_requirements, write_build_requirements

# What the camel reactor summary states, in the build system's own words.
CAMEL_OUTCOMES = [
    {"module": "Apache Camel :: Core", "status": "SUCCESS"},
    {"module": "Apache Camel :: JMS", "status": "FAILURE"},
    {"module": "Apache Camel :: FTP", "status": "SKIPPED"},
]

# The survey's guess for polaris, verbatim in shape.
BLIND_SURVEY = {
    "root_shape": "single_module",
    "build_islands": [],
    "java_version": "17",
}


class ManifestOrchestrator:
    """A container holding one build-requirements file."""

    def __init__(self, manifest=None):
        self.manifest = json.dumps(manifest, sort_keys=True) if manifest is not None else None
        self.commands = []
        self.calls = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        self.calls.append((command, kwargs))
        text = command.strip()
        if text.startswith("cat ") and "<<" not in text and BUILD_REQUIREMENTS_PATH in text:
            if self.manifest is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.manifest}
        if "<<'SAG_STRUCTURE_EOF'" in text or "<<'SAGEOF'" in text:
            marker = "SAG_STRUCTURE_EOF" if "SAG_STRUCTURE_EOF" in text else "SAGEOF"
            self.manifest = text.split("\n", 1)[1].rsplit(f"\n{marker}", 1)[0]
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}

    def stored(self):
        return json.loads(self.manifest) if self.manifest else {}


def _receipt(
    receipt_id="inv-maven-1-0001",
    *,
    exit_code=0,
    outcomes=CAMEL_OUTCOMES,
    lifecycle_state=None,
    termination_reason=None,
):
    receipt = {"receipt_id": receipt_id, "module_outcomes": list(outcomes)}
    if exit_code is not None:
        receipt["exit_code"] = exit_code
    if lifecycle_state is not None:
        receipt["lifecycle_state"] = lifecycle_state
    if termination_reason is not None:
        receipt["termination_reason"] = termination_reason
    return receipt


V1_RECEIPT_ARGS = dict(
    receipt_id="inv-gradle-1-0007",
    tool="gradle",
    requested_action="build",
    effective_action="build",
    argv="/workspace/camel/gradlew build",
    working_directory="/workspace/camel",
    exit_code=1,
    before={},
    after={},
)


# ---------------------------------------------------------------------------
# what a receipt proves
# ---------------------------------------------------------------------------
def test_a_terminal_receipt_that_named_its_modules_proves_the_structure():
    structure = structure_from_receipt(_receipt())

    assert structure["provenance"] == "inv-maven-1-0001"
    assert structure["modules"] == [entry["module"] for entry in CAMEL_OUTCOMES]
    assert structure["keys"] == ["core", "jms", "ftp"]


def test_the_keys_are_the_coverage_matcher_s_own_formula():
    """One spelling for the receipt, the manifest and the denominator, or the
    two ends of the match silently stop meeting."""
    for label in ("Apache Camel :: Core", "/workspace/camel/core", "build-logic"):
        assert module_key(label) == PhysicalValidator._module_key(label)


def test_a_dispatch_with_no_terminal_exit_proves_nothing():
    """A job still in flight has stated nothing yet — the exact case §3.2
    settles later. Structure is never guessed from a partial log."""
    assert structure_from_receipt(_receipt(exit_code=None)) is None


def test_a_receipt_that_named_no_modules_proves_nothing():
    """polaris's one receipt, the failed Java-17 compile: no module_outcomes,
    so it states no structure and the survey's proposal stands."""
    assert structure_from_receipt(_receipt(outcomes=[])) is None


def test_a_failed_build_still_states_what_the_project_is_made_of():
    """Exit 1 is a verdict about the BUILD, not about the reactor's shape. The
    camel receipt that exited 1 still named the modules it walked."""
    assert structure_from_receipt(_receipt(exit_code=1))["keys"] == ["core", "jms", "ftp"]


# ---------------------------------------------------------------------------
# terminality is a property of how the dispatch ENDED, not of the exit code
# ---------------------------------------------------------------------------
def test_a_crashed_detached_job_proves_nothing_though_it_carries_an_exit_code():
    """The OOM case, live on this machine.

    `execute_command_with_soft_timeout` polls a job whose process is gone;
    `collect_detached_result` SYNTHESIZES `exit_code = 1` and reports
    `lifecycle_state: 'vanished'`, and `dispatch_status: 'completed_detached'`
    is NOT in DETACHED_HANDOFF_STATUSES — so the runner writes an ordinary
    receipt whose `module_outcomes` were parsed from a log the kill truncated.
    Gradle prints `> Task :m:compileJava` incrementally, so a 300-module build
    killed at module 40 names exactly 40. "exit_code is an int" said terminal;
    the dispatch never terminated.
    """
    killed = _receipt(
        "inv-gradle-1-0007",
        exit_code=1,
        outcomes=[{"module": f"m{index}", "status": "SUCCESS"} for index in range(40)],
        lifecycle_state="vanished",
    )

    assert structure_from_receipt(killed) is None

    orch = ManifestOrchestrator(BLIND_SURVEY)
    assert promote_structure(orch.execute_command, killed) is False
    assert orch.stored() == BLIND_SURVEY


def test_a_dispatch_something_else_stopped_proves_nothing():
    """A soft/absolute timeout kill states its reason, and the log stops where
    the kill landed: its module list is a prefix of the build, not a statement
    about the project."""
    killed = _receipt(exit_code=143, termination_reason="silent_timeout")

    assert structure_from_receipt(killed) is None


def test_a_receipt_records_how_its_dispatch_ended():
    """A reader could not tell a synthesized exit code from a recorded one —
    neither fact rode the receipt. Now both do, and absent still means unknown
    (every receipt written before this design reads exactly as it did)."""
    crashed = build_receipt(**V1_RECEIPT_ARGS, lifecycle_state="vanished")
    timed_out = build_receipt(**V1_RECEIPT_ARGS, termination_reason="absolute_timeout")

    assert crashed["lifecycle_state"] == "vanished"
    assert timed_out["termination_reason"] == "absolute_timeout"
    assert "lifecycle_state" not in build_receipt(**V1_RECEIPT_ARGS)
    assert "termination_reason" not in build_receipt(**V1_RECEIPT_ARGS)


def test_a_receipt_that_finished_normally_still_proves_its_structure():
    """The regression fence for the guard above: a detached job that DID write
    its own exit code is terminal, and a synchronous dispatch states no
    lifecycle at all — both keep promoting exactly as before."""
    assert structure_from_receipt(_receipt(lifecycle_state="finished"))["keys"] == [
        "core",
        "jms",
        "ftp",
    ]
    assert structure_from_receipt(_receipt())["keys"] == ["core", "jms", "ftp"]


def test_the_runner_puts_the_dispatch_lifecycle_on_the_receipt_it_writes():
    """The link between the two: a vanished dispatch's own facts reach
    `record_invocation`, so the promotion guard has something to read."""
    captured = {}

    class StubOrchestrator:
        def execute_command(self, command, **_kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    from sag.tools.internal import gradle_tool as gradle_module
    from sag.tools.internal import maven_tool as maven_module

    vanished = {
        "exit_code": 1,
        "output": "> Task :core:compileJava",
        "full_output": "> Task :core:compileJava",
        "dispatch_status": "completed_detached",
        "lifecycle_state": "vanished",
        "termination_reason": None,
    }
    for module, tool_class, kwargs in (
        (gradle_module, gradle_module.GradleTool, {}),
        (maven_module, maven_module.MavenTool, {"effective_action": "test"}),
    ):
        original = module.record_invocation
        module.record_invocation = lambda _execute, **fields: captured.update(fields) or {}
        try:
            tool = tool_class(StubOrchestrator())
            tool._record_invocation_receipt(
                requested_action="test",
                argv="./gradlew test",
                working_directory="/workspace/camel",
                attempt=1,
                result=vanished,
                before={},
                **kwargs,
            )
        finally:
            module.record_invocation = original
        assert captured["lifecycle_state"] == "vanished"
        assert captured["exit_code"] == 1


# ---------------------------------------------------------------------------
# the ladder, persisted
# ---------------------------------------------------------------------------
def test_the_first_terminal_receipt_persists_the_structure_at_receipt_provenance():
    orch = ManifestOrchestrator(BLIND_SURVEY)

    assert promote_structure(orch.execute_command, _receipt()) is True

    stored = orch.stored()
    assert stored["root_shape"] == "single_module"  # the survey's own fields survive
    assert stored[STRUCTURE_KEY]["provenance"] == "inv-maven-1-0001"
    assert stored[STRUCTURE_KEY]["keys"] == ["core", "jms", "ftp"]


def test_the_same_receipt_twice_is_a_no_op():
    """Same-body-no-op, the convention every evidence writer here follows."""
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())
    writes = len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c])

    assert promote_structure(orch.execute_command, _receipt()) is False
    assert len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c]) == writes


def test_a_newer_terminal_receipt_may_widen_the_structure():
    """§3.6's "may update": a receipt that walked modules nobody had seen states
    a wider project, and that is new knowledge."""
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())

    promoted = promote_structure(
        orch.execute_command,
        _receipt(
            "inv-maven-2-0004",
            outcomes=[*CAMEL_OUTCOMES, {"module": "Apache Camel :: HTTP", "status": "SUCCESS"}],
        ),
    )

    assert promoted is True
    assert orch.stored()[STRUCTURE_KEY]["provenance"] == "inv-maven-2-0004"
    assert orch.stored()[STRUCTURE_KEY]["keys"] == ["core", "jms", "ftp", "http"]


def test_a_scoped_receipt_may_not_narrow_a_wider_proven_structure():
    """`mvn -pl core` states what the dispatch ATTEMPTED, not what the project
    is made of. Replacing unconditionally let that narrow statement demote a
    full reactor's — and a structure is only ever read as a statement about the
    project, so a subset proves nothing new and the wider statement stands."""
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())
    writes = len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c])

    scoped = promote_structure(
        orch.execute_command,
        _receipt(
            "inv-maven-2-0002",
            outcomes=[{"module": "Apache Camel :: Core", "status": "SUCCESS"}],
        ),
    )

    assert scoped is False
    assert len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c]) == writes
    assert orch.stored()[STRUCTURE_KEY]["provenance"] == "inv-maven-1-0001"
    assert orch.stored()[STRUCTURE_KEY]["keys"] == ["core", "jms", "ftp"]


def test_a_disjoint_receipt_may_not_replace_a_wider_proven_structure():
    """§3.6 (revised): "Only a terminal receipt whose statement is at least as
    wide may restate the structure."

    `structure_updates` promised WIDEN-or-nothing and delivered
    not-a-subset-therefore-replace. A dispatch that walked one module nobody had
    seen (`mvn -pl http`, or a second build island) is disjoint from the proven
    three, so it is not a subset — and it replaced a three-module proven fact with
    a one-module one, narrowing the persisted receipt-proven structure in exactly
    the direction the subset guard existed to forbid. Neither is a union the
    answer: no receipt ever stated one, and the provenance would name a receipt
    for a list it did not state.
    """
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())
    writes = len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c])

    disjoint = promote_structure(
        orch.execute_command,
        _receipt(
            "inv-maven-3-0003",
            outcomes=[{"module": "Apache Camel :: HTTP", "status": "SUCCESS"}],
        ),
    )
    overlapping = promote_structure(
        orch.execute_command,
        _receipt(
            "inv-maven-4-0004",
            outcomes=[
                {"module": "Apache Camel :: Core", "status": "SUCCESS"},
                {"module": "Apache Camel :: HTTP", "status": "SUCCESS"},
            ],
        ),
    )

    assert (disjoint, overlapping) == (False, False)
    assert len([c for c in orch.commands if "SAG_STRUCTURE_EOF" in c]) == writes
    assert orch.stored()[STRUCTURE_KEY]["provenance"] == "inv-maven-1-0001"
    assert orch.stored()[STRUCTURE_KEY]["keys"] == ["core", "jms", "ftp"]


def test_only_an_at_least_as_wide_statement_restates_the_structure():
    """The predicate itself, over the four shapes a second receipt can have."""
    proven = {"keys": ["core", "jms", "ftp"]}

    assert structure_updates(proven, {"keys": ["core", "jms", "ftp", "http"]}) is True
    assert structure_updates({}, {"keys": ["core"]}) is True
    assert structure_updates(proven, {"keys": ["core", "jms", "ftp"]}) is False
    assert structure_updates(proven, {"keys": ["core"]}) is False
    assert structure_updates(proven, {"keys": ["http"]}) is False
    assert structure_updates(proven, {"keys": ["core", "http"]}) is False
    assert structure_updates(proven, {"keys": []}) is False


def test_a_manifest_that_could_not_be_read_whole_is_never_rewritten():
    """The write is a read-MODIFY-write of the survey's ENTIRE manifest.

    Treating an unparseable body as an empty manifest replaced every stated
    requirement — java_version, build_islands, the survey pins — with one
    structure key. A manifest we could not read is not a manifest we may
    rewrite.
    """
    mangled = "...[output truncated]...\n" + json.dumps(BLIND_SURVEY)
    orch = ManifestOrchestrator(BLIND_SURVEY)
    orch.manifest = mangled

    assert promote_structure(orch.execute_command, _receipt()) is False
    assert orch.manifest == mangled
    assert [c for c in orch.commands if "SAG_STRUCTURE_EOF" in c] == []


def test_the_manifest_is_read_through_the_lossless_path():
    """DockerOrchestrator strips and may TRUNCATE ordinary command output before
    it reaches the model; `sag/runtime/container_io.py` exists so machine
    consumers bypass that, and a read-modify-write of the survey's manifest is
    exactly such a consumer. A bare `cat` on the presentation path is how a
    large manifest comes back mangled in the first place."""
    orch = ManifestOrchestrator(BLIND_SURVEY)

    promote_structure(orch.execute_command, _receipt())

    reads = [
        kwargs
        for command, kwargs in orch.calls
        if BUILD_REQUIREMENTS_PATH in command and "SAG_STRUCTURE_EOF" not in command
    ]
    assert reads
    assert all(kwargs.get("truncate_output") is False for kwargs in reads)


def test_a_container_with_no_manifest_yet_gets_one():
    """Creating is not clobbering: absent and unreadable are different facts."""
    orch = ManifestOrchestrator()

    assert promote_structure(orch.execute_command, _receipt()) is True
    assert orch.stored()[STRUCTURE_KEY]["keys"] == ["core", "jms", "ftp"]


def test_a_survey_rerun_may_never_demote_a_receipt_proven_structure():
    """The analyzer rewrites the whole manifest. A second survey has the same
    blind spot as the first, and must not erase what real work proved."""
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())

    assert write_build_requirements(orch, dict(BLIND_SURVEY)) is True

    assert read_module_structure(orch.stored())["provenance"] == "inv-maven-1-0001"


def test_a_manifest_written_before_this_design_proves_no_structure():
    """Additive: an older manifest carries no key and every reader degrades to
    the survey's proposal, which is exactly today's behaviour."""
    assert read_module_structure(BLIND_SURVEY) == {}
    assert read_module_structure(None) == {}


# ---------------------------------------------------------------------------
# one writer: the receipt path itself
# ---------------------------------------------------------------------------
def test_recording_a_receipt_promotes_the_structure_it_just_stated():
    """No second bookkeeping system: the structure lands where the receipt
    lands, so a settled receipt (§3.2) promotes exactly like a synchronous one."""
    orch = ManifestOrchestrator(BLIND_SURVEY)

    record_invocation(
        orch.execute_command,
        tool="maven",
        attempt=1,
        requested_action="compile",
        effective_action="compile",
        argv="mvn compile",
        working_directory="/workspace/camel",
        exit_code=0,
        before={},
        after={},
        module_outcomes=CAMEL_OUTCOMES,
    )

    assert read_module_structure(orch.stored())["keys"] == ["core", "jms", "ftp"]


def test_a_receipt_that_stated_no_modules_leaves_the_manifest_alone():
    orch = ManifestOrchestrator(BLIND_SURVEY)

    record_invocation(
        orch.execute_command,
        tool="gradle",
        attempt=1,
        requested_action="build",
        effective_action="build",
        argv="./gradlew build",
        working_directory="/workspace/polaris",
        exit_code=1,
        before={},
        after={},
    )

    assert orch.stored() == BLIND_SURVEY


# ---------------------------------------------------------------------------
# what the denominator does with it
# ---------------------------------------------------------------------------
def test_the_denominator_stands_on_the_proven_structure_when_this_phase_stated_nothing():
    """A structure proved in the build phase is still proved in the test phase.
    Without this the denominator falls back to the survey guess the moment the
    current phase's dispatches are receipt-free."""
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")

    structure = validator._receipt_structure()

    assert structure["provenance"] == "inv-maven-1-0001"
    assert read_build_requirements(orch)[STRUCTURE_KEY]["modules"] == structure["modules"]
