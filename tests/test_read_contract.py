# tests/test_read_contract.py
"""Plan 8 §3.9, first half — a read that did not succeed is not a read that
found nothing.

Live-shaped evidence, twice over. The integration of lanes A and B failed
exactly one test, and the walk found this chain: gate-time settlement wrote a
receipt, `record_invocation` called `promote_structure`, its manifest read came
back None on a NOT-SUCCEEDED command, and "absent → create" then replaced the
whole survey manifest with one `module_structure` key — `build_islands`,
`build_system`, `survey` gone, the untried-islands rule blind. An external
review (codex, 2026-07-30) then reproduced the same wipe against the REAL
`DockerOrchestrator` with a controlled transient failure: two consecutive
failed reads followed by a recovered write destroy the live manifest too.

`DockerOrchestrator.execute_command` converts every within-command failure
into `{"success": False, "exit_code": -1, ...}` — it does not raise for those
— so a `try/except` around the read can never see the failure. The distinction
must live in the read itself: on the `exact_bytes` path, None is reserved for
MARKER-VERIFIED absence (`__SAG_FILE_MISSING__` / exit 44), and a command that
did not succeed raises `ContainerFileReadError`, which the function's own
docstring promised all along.

Scope, deliberately: the `exact_bytes` transport path only. `_direct_read` and
the non-exact readers keep today's semantics this round — the review measured
17 test doubles that express "absent" as a plain failure, and migrating them
is its own commit. The contract claim is scoped accordingly.
"""

import json

import pytest

from sag.agent.receipt_structure import promote_structure
from sag.runtime.container_io import ContainerFileReadError, read_container_text
from sag.tools.internal.build_preflight import (
    BUILD_REQUIREMENTS_PATH,
    write_build_requirements,
)

MANIFEST = {
    "build_islands": [{"root": "/workspace/polaris/polaris-core", "system": "gradle"}],
    "build_system": "gradle",
    "survey": {"project_path": "/workspace/polaris"},
}

TERMINAL_RECEIPT = {
    "receipt_id": "inv-gradle-1-0002",
    "exit_code": 0,
    "lifecycle_state": "finished",
    "module_outcomes": [
        {"module": "core", "status": "attempted"},
        {"module": "jms", "status": "attempted"},
    ],
}


def ok(output="", exit_code=0):
    return {"success": True, "exit_code": exit_code, "output": output}


def fail(output="", exit_code=1):
    return {"success": False, "exit_code": exit_code, "output": output}


class Transport:
    """A container surface that speaks the base64 transport protocol.

    `mode` selects what the transport probe reports:
      present     — marker + base64 payload (healthy production)
      absent      — MISSING marker, exit 44 (verified absence)
      failing     — the exact dict DockerOrchestrator returns on an internal
                    exception; every subsequent command fails the same way
                    (the two-consecutive-failures window the review measured)
    """

    def __init__(self, mode, body=None):
        self.mode = mode
        self.body = body if body is not None else json.dumps(MANIFEST)
        self.files = None  # never offer the in-memory shortcut
        self.writes = []

    def execute_command(self, command, **kwargs):
        if self.mode == "failing":
            # The review's live reproduction: the READS fail transiently, the
            # WRITE then succeeds. A fake whose writes also fail would let the
            # fences pass for the wrong reason — `promoted is False` because
            # the write failed, not because the read refused.
            if command.startswith("mkdir"):
                return ok()
            if "mv " in command or command.startswith("cat >"):
                self.writes.append(command)
                return ok()
            return {
                "success": False,
                "exit_code": -1,
                "output": "Failed to execute command: transport hiccup",
                "dispatch_status": "dispatch_failed",
            }
        if command.startswith("if test -f"):
            if self.mode == "absent":
                return fail("__SAG_FILE_MISSING__", exit_code=44)
            import base64 as b64

            payload = b64.b64encode(self.body.encode()).decode()
            return ok(f"__SAG_FILE_BASE64__{payload}")
        if "mv " in command or command.startswith("cat >"):
            self.writes.append(command)
            return ok()
        return ok()


# ---------------------------------------------------------------------------
# the read boundary
# ---------------------------------------------------------------------------


def test_a_not_succeeded_read_raises_instead_of_reporting_absence():
    with pytest.raises(ContainerFileReadError):
        read_container_text(
            Transport("failing"), BUILD_REQUIREMENTS_PATH, exact_bytes=True
        )


def test_marker_verified_absence_is_still_none():
    assert (
        read_container_text(Transport("absent"), BUILD_REQUIREMENTS_PATH, exact_bytes=True)
        is None
    )


def test_a_healthy_transport_read_returns_the_exact_bytes():
    content = read_container_text(
        Transport("present"), BUILD_REQUIREMENTS_PATH, exact_bytes=True
    )

    assert json.loads(content) == MANIFEST


def test_a_compat_double_that_serves_cat_still_works():
    """A double that speaks no transport but answers a plain `cat --` keeps
    working: the fall-through fires only when the probe SUCCEEDED unmarked."""

    class CatOnly:
        files = None

        def execute_command(self, command, **kwargs):
            if command.startswith("cat -- "):
                return ok(json.dumps(MANIFEST))
            return ok()

    content = read_container_text(CatOnly(), BUILD_REQUIREMENTS_PATH, exact_bytes=True)

    assert json.loads(content) == MANIFEST


def test_a_compat_double_that_fails_the_cat_raises_on_the_exact_path():
    """A plain `cat` cannot prove absence, so its failure is a failed READ.
    This is the integration wipe's exact shape: the probe answered ok('')
    (unknown command), the `cat --` failed, and None then read as 'absent'."""

    class UnknownCommands:
        files = None

        def execute_command(self, command, **kwargs):
            if command.startswith("cat -- "):
                return fail("cat: --: No such file or directory")
            return ok()

    with pytest.raises(ContainerFileReadError):
        read_container_text(UnknownCommands(), BUILD_REQUIREMENTS_PATH, exact_bytes=True)


def test_a_failed_probe_never_degrades_to_the_lossy_cat():
    """The exact path exists because plain `cat` strips bytes. A probe that
    did not succeed must raise, not silently hand back a stripped read that
    would fail byte-identity later anyway (env_overlay's readback) or feed a
    subtly different body to a transactional writer."""

    class ProbeFailsCatWorks:
        files = None

        def execute_command(self, command, **kwargs):
            if command.startswith("if test -f"):
                return fail("Failed to execute command: transport hiccup", exit_code=-1)
            if command.startswith("cat -- "):
                return ok(json.dumps(MANIFEST))
            return ok()

    with pytest.raises(ContainerFileReadError):
        read_container_text(
            ProbeFailsCatWorks(), BUILD_REQUIREMENTS_PATH, exact_bytes=True
        )


def test_the_non_exact_path_keeps_its_none_on_failure_semantics():
    """Scoped on purpose: parsers and read-only consumers migrate in their own
    commit; this round must not change their control flow."""

    class FailsEverything:
        files = None

        def execute_command(self, command, **kwargs):
            return fail("boom")

    assert read_container_text(FailsEverything(), "/x") is None


# ---------------------------------------------------------------------------
# the direct-read (test-double) surface speaks the same contract
# ---------------------------------------------------------------------------
#
# `read_file` exists ONLY on test doubles — production DockerOrchestrator has
# no such method, so production always takes the transport path above. The
# doubles' protocol still has to state the same three answers, or every test
# exercises a contract production does not have: None = absent, a mapping
# that did not succeed = a failed READ (raises on the exact path), content
# otherwise.


def test_a_direct_read_failure_raises_on_the_exact_path():
    class FailingReadFile:
        def read_file(self, path):
            return {"success": False, "content": "", "exit_code": 1}

    with pytest.raises(ContainerFileReadError):
        read_container_text(FailingReadFile(), "/x", exact_bytes=True)


def test_a_direct_read_none_still_means_absent():
    class AbsentReadFile:
        def read_file(self, path):
            return None

    assert read_container_text(AbsentReadFile(), "/x", exact_bytes=True) is None


def test_a_direct_read_failure_keeps_none_on_the_non_exact_path():
    """Same scoping as the transport path: parsers migrate separately."""

    class FailingReadFile:
        def read_file(self, path):
            return {"success": False, "content": "", "exit_code": 1}

    assert read_container_text(FailingReadFile(), "/x") is None


# ---------------------------------------------------------------------------
# the two manifest fences (the defect this closes)
# ---------------------------------------------------------------------------


def test_a_failed_read_writes_nothing_and_the_manifest_survives():
    """The integration wipe, refused: no write may follow a read that did not
    succeed. Byte-identity is the whole assertion."""
    surface = Transport("failing")

    promoted = promote_structure(surface.execute_command, TERMINAL_RECEIPT)

    assert promoted is False
    assert surface.writes == []


def test_marker_verified_absence_still_creates_the_manifest():
    """First-ever creation stays legal: absence PROVEN by the transport marker
    is the one None that may create."""
    surface = Transport("absent")

    promoted = promote_structure(surface.execute_command, TERMINAL_RECEIPT)

    assert promoted is True
    assert len(surface.writes) == 1
    assert "module_structure" in surface.writes[0]


def test_a_write_refused_on_an_unreadable_manifest_refuses_the_whole_write():
    """`write_build_requirements` read-modify-writes the manifest to preserve a
    receipt-proven structure. When the read did not succeed it cannot know
    whether one exists, so the conservative act is to not write at all."""
    surface = Transport("failing")

    written = write_build_requirements(surface, {"build_system": "gradle"})

    assert written is False
    assert surface.writes == []


def test_a_verified_absent_manifest_is_still_writable():
    surface = Transport("absent")

    written = write_build_requirements(surface, {"build_system": "gradle"})

    assert written is True
    assert len(surface.writes) == 1


# ---------------------------------------------------------------------------
# §6.8 fence 1 — an unreadable obligations ledger HOLDS the §3.3 cap
# ---------------------------------------------------------------------------
#
# The round-four review attacked P4 four ways — delete the obligation file,
# corrupt it, make the ledger cat raise, make settlement raise — and every one
# upgraded a contradicted success to a confirmed one, because a failed ledger
# read parsed as "no obligations". Removing evidence improved the verdict.
#
# "Could not read the ledger" and "read it, it is empty" are told apart by the
# one unambiguous signature DockerOrchestrator's failure dict carries
# (exit -1 / dispatch_status): a glob cat over an empty directory exits 1,
# which stays "no obligations" for every existing double.

from sag.agent.job_obligations import LEDGER_UNREADABLE, read_obligations


class LedgerSurface:
    """Serves the obligations glob according to `mode`."""

    def __init__(self, mode):
        self.mode = mode

    def execute_command(self, command, **kwargs):
        if "job_obligations" in command and command.startswith("cat "):
            if self.mode == "empty":
                return fail("", exit_code=1)  # glob matched nothing: ran, empty
            if self.mode == "transport":
                return {
                    "success": False,
                    "exit_code": -1,
                    "output": "Failed to execute command: transport hiccup",
                    "dispatch_status": "dispatch_failed",
                }
            if self.mode == "raises":
                raise RuntimeError("Container missing does not exist. Create it first.")
        return ok()


def test_an_empty_ledger_reads_as_no_obligations():
    assert read_obligations(LedgerSurface("empty")) == []


def test_a_transport_failure_reads_as_could_not_read():
    assert read_obligations(LedgerSurface("transport")) is None


def test_a_raised_container_error_reads_as_could_not_read():
    """The pre-command container checks DO raise (the review's correction to
    'execute_command never raises'); that is a failed read too."""
    assert read_obligations(LedgerSurface("raises")) is None


def test_an_unreadable_ledger_holds_the_gate_cap(monkeypatch):
    """The P4 fence: with the ledger unreadable, a GREEN success claim is NOT
    confirmed — the cap holds on a stated inability, never lifts on one."""
    from sag.agent import phase_gates
    from sag.agent.phase_gates import (
        OPEN_OBLIGATIONS_FACT,
        _inspect_phase,
        _ValidatorObservation,
        ValidatorState,
    )

    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda phase, validator, orchestrator, project_name: _ValidatorObservation(
            ValidatorState.GREEN,
            reason="build verified",
            code="build_verified",
            validated_facts={"build.test_entry_ready": True},
        ),
    )

    observation = _inspect_phase("build", None, LedgerSurface("transport"), "polaris")

    assert observation.validated_facts[OPEN_OBLIGATIONS_FACT] == [LEDGER_UNREADABLE]


def test_a_readable_empty_ledger_states_no_fact(monkeypatch):
    """The other direction: genuinely-no-obligations changes nothing."""
    from sag.agent import phase_gates
    from sag.agent.phase_gates import (
        OPEN_OBLIGATIONS_FACT,
        _inspect_phase,
        _ValidatorObservation,
        ValidatorState,
    )

    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda phase, validator, orchestrator, project_name: _ValidatorObservation(
            ValidatorState.GREEN,
            reason="build verified",
            code="build_verified",
            validated_facts={"build.test_entry_ready": True},
        ),
    )

    observation = _inspect_phase("build", None, LedgerSurface("empty"), "polaris")

    assert OPEN_OBLIGATIONS_FACT not in observation.validated_facts


def test_the_unreadable_ledger_refusal_names_the_ledger():
    from sag.agent.phase_gates import OPEN_OBLIGATIONS_FACT, validate_phase_claim
    from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
    from sag.agent.phase_gates import ValidatorState

    gate = validate_phase_claim(
        PhaseClaim(phase="build", signal="done", claimed_outcome=PhaseOutcome.SUCCESS),
        ValidatorState.GREEN,
        validated_facts={OPEN_OBLIGATIONS_FACT: [LEDGER_UNREADABLE]},
    )

    assert gate.accepted is False
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert "ledger" in gate.reason


# ---------------------------------------------------------------------------
# the loss is bounded: the next terminal receipt recovers the promotion
# ---------------------------------------------------------------------------


def test_a_failed_promotion_is_recovered_by_the_next_terminal_receipt():
    """Settlement is idempotent, so a promotion lost to a transient failure is
    not retried for THAT obligation — accepted, fail-closed. What bounds the
    loss: every later terminal receipt runs the same promotion, so the
    structure lands with the next real dispatch."""
    surface = Transport("failing")
    assert promote_structure(surface.execute_command, TERMINAL_RECEIPT) is False

    surface.mode = "absent"  # the transient failure cleared; manifest absent
    recovered = promote_structure(surface.execute_command, TERMINAL_RECEIPT)

    assert recovered is True
    assert len(surface.writes) == 1
