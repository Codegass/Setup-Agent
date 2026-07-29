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

from sag.agent.invocation_receipts import record_invocation
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_structure import (
    STRUCTURE_KEY,
    module_key,
    promote_structure,
    read_module_structure,
    structure_from_receipt,
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

    def execute_command(self, command, **_kwargs):
        self.commands.append(command)
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


def _receipt(receipt_id="inv-maven-1-0001", *, exit_code=0, outcomes=CAMEL_OUTCOMES):
    receipt = {"receipt_id": receipt_id, "module_outcomes": list(outcomes)}
    if exit_code is not None:
        receipt["exit_code"] = exit_code
    return receipt


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


def test_a_newer_terminal_receipt_may_restate_the_structure():
    orch = ManifestOrchestrator(BLIND_SURVEY)
    promote_structure(orch.execute_command, _receipt())

    promoted = promote_structure(
        orch.execute_command,
        _receipt("inv-maven-2-0004", outcomes=[{"module": "core", "status": "SUCCESS"}]),
    )

    assert promoted is True
    assert orch.stored()[STRUCTURE_KEY]["provenance"] == "inv-maven-2-0004"
    assert orch.stored()[STRUCTURE_KEY]["modules"] == ["core"]


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
