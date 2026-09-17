"""The derivation, held to three sessions the engine really wrote.

Every payload replayed here came out of a real run's `control_events.jsonl`.
That is the point of the file: the summarisers were first written against
invented fixtures, and four whole-tool branches were wrong on 100% of real data
while a green suite said nothing — the tool was called `file_io` and the branch
said `files`, a build envelope carries `action` where the branch read `command`,
a phase result keeps its gate word in `metadata.gate_result` where the branch
read `facts`, and no real `invocation_status` matched the three words the
pending check looked for.

So the counts below are literals. A branch that goes dead again takes a number
with it, and this file fails. Nothing here asserts that *something* was
produced; every count says how many, and the exact-value test says what a reader
will actually read.
"""

import json
from pathlib import Path

import pytest

from sag.trajectory.schema import SUMMARY_MAX_CHARS
from sag.trajectory.summaries import call_summary, observation_outcome, observation_summary

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trajectory"
SESSIONS = ("camel-quarkus-d2r3", "ignite-d2r3", "kafka-d2r3")

#: A call is stated by an envelope the model asked for, or by one the harness
#: forced. Both carry `exact_params`, and both are turns a reader sees.
CALL_KINDS = ("action_envelope", "forced_action")

#: Envelopes per tool, per session. `call_summary` must name every one of them:
#: a tool present in a session and absent from this table is a tool this fence
#: stopped watching.
ENVELOPES_PER_TOOL = {
    "camel-quarkus-d2r3": {
        "advisor": 2,
        "bash": 9,
        "build": 2,
        "phase": 14,
        "project": 6,
        "report": 1,
        "search": 21,
    },
    "ignite-d2r3": {"advisor": 3, "bash": 3, "build": 2, "phase": 6, "project": 3, "search": 18},
    "kafka-d2r3": {
        "advisor": 2,
        "bash": 1,
        "build": 2,
        "phase": 7,
        "project": 4,
        "report": 1,
        "search": 7,
    },
}

#: Results per tool, per session.
RESULTS_PER_TOOL = {session: dict(tools) for session, tools in ENVELOPES_PER_TOOL.items()}

#: Results whose observation summary says something, per tool. Unlike a call, a
#: result may honestly have nothing to state — a search that matched and a bash
#: that exited 0 carry no line of their own — so these are coverage floors held
#: exactly, not totals. `phase` and `build` at full count are the two that
#: silently fell to zero before.
OBSERVATIONS_NAMED_PER_TOOL = {
    "camel-quarkus-d2r3": {"advisor": 2, "bash": 6, "build": 2, "phase": 14, "project": 6},
    "ignite-d2r3": {"advisor": 3, "bash": 1, "build": 2, "phase": 6, "project": 3, "search": 1},
    "kafka-d2r3": {"advisor": 2, "build": 2, "phase": 7, "project": 3},
}

#: `(session, control sequence) -> the line a reader is shown`. Copied out of
#: the archived ledgers, so each one states a real command, a real ref, a real
#: gate word, a real job handle.
REAL_CALL_LINES = {
    ("camel-quarkus-d2r3", 3): "clone apache/camel-quarkus@3.36.0",
    ("camel-quarkus-d2r3", 11): "done success",
    ("camel-quarkus-d2r3", 61): "consult",
    ("camel-quarkus-d2r3", 63): "compile",
    ("camel-quarkus-d2r3", 89): "test",
    ("camel-quarkus-d2r3", 254): "generate",
    ("ignite-d2r3", 6): "clone apache/ignite@2.18.0",
    ("ignite-d2r3", 17): "name:/usr|name:/usr/share|name:/opt /mvn|mvnw/",
    ("ignite-d2r3", 23): "find / -path '*/bin/mvn' 2>/dev/null | head -20",
    ("ignite-d2r3", 156): "note",
    # A forced action: the harness asked for this one, and it reads like any turn.
    ("ignite-d2r3", 235): "test",
    ("kafka-d2r3", 3): "clone apache/kafka@4.3.1",
    ("kafka-d2r3", 7): "provision java 17",
    ("kafka-d2r3", 11): "env gradle",
}

REAL_OBSERVATION_LINES = {
    ("camel-quarkus-d2r3", 4): "5dec869 → /workspace/camel-quarkus",
    (
        "camel-quarkus-d2r3",
        12,
    ): "gate workspace_present · workspace /workspace/camel-quarkus exists",
    ("camel-quarkus-d2r3", 62): "advice delivered",
    ("camel-quarkus-d2r3", 70): "exit 1 · JAVA_VERSION_ERROR",
    ("camel-quarkus-d2r3", 99): "ENV_MAVEN_EXECUTABLE_NAME_MISMATCH",
    ("ignite-d2r3", 18): "SEARCH_FAILED",
    ("ignite-d2r3", 157): "note",
    ("ignite-d2r3", 174): "exit 0 · 5 jars",
    # The one dispatched job in the three sessions: pending, not failed.
    ("ignite-d2r3", 238): "running · job 2c4d56b2fdca",
    ("kafka-d2r3", 4): "26b251a → /workspace/kafka",
    ("kafka-d2r3", 124): "exit 0",
    ("kafka-d2r3", 143): "exit 1 · DETACHED_OPERATION_FAILED",
}


def _events(session: str):
    ledger = FIXTURE_DIR / session / "control_events.jsonl"
    assert ledger.exists(), f"archived session {session} is missing its ledger"
    for line in ledger.read_text().splitlines():
        if line.strip():
            yield json.loads(line)


def _calls(session: str):
    """`(sequence, tool, exact_params)` for every call the session states."""

    for event in _events(session):
        if event.get("kind") in CALL_KINDS:
            payload = event.get("payload") or {}
            yield event.get("sequence"), payload.get("tool"), payload.get("exact_params")


def _results(session: str):
    """`(sequence, tool, result)` for every tool result the session states."""

    for event in _events(session):
        if event.get("kind") == "tool_result":
            payload = event.get("payload") or {}
            yield event.get("sequence"), payload.get("tool"), payload.get("result")


def _is_a_line(value):
    return isinstance(value, str) and value.strip() == value and 0 < len(value) <= SUMMARY_MAX_CHARS


@pytest.mark.parametrize("session", SESSIONS)
def test_every_real_call_is_named(session):
    """A tool that appears in a session is summarised on every call it made."""

    named = {}
    total = {}
    for sequence, tool, params in _calls(session):
        total[tool] = total.get(tool, 0) + 1
        summary = call_summary(tool, params)
        assert summary is not None, f"{session} seq {sequence}: {tool} call has no summary"
        assert _is_a_line(summary), f"{session} seq {sequence}: {summary!r} is not one line"
        named[tool] = named.get(tool, 0) + 1

    assert total == ENVELOPES_PER_TOOL[session]
    assert named == ENVELOPES_PER_TOOL[session]


@pytest.mark.parametrize("session", SESSIONS)
def test_every_real_result_is_read_without_raising(session):
    """No real payload makes a summariser raise, and none yields a blank line."""

    total = {}
    named = {}
    for sequence, tool, result in _results(session):
        total[tool] = total.get(tool, 0) + 1
        outcome = observation_outcome(result)
        summary = observation_summary(tool, result)
        assert outcome is None or isinstance(outcome, str)
        if summary is not None:
            assert _is_a_line(summary), f"{session} seq {sequence}: {summary!r} is not one line"
            named[tool] = named.get(tool, 0) + 1

    assert total == RESULTS_PER_TOOL[session]
    assert named == OBSERVATIONS_NAMED_PER_TOOL[session]


@pytest.mark.parametrize("session", SESSIONS)
def test_a_real_success_is_never_read_as_a_failure(session):
    """And a dispatched job is read as running, not as a build that failed."""

    pending = 0
    for sequence, tool, result in _results(session):
        payload = result or {}
        outcome = observation_outcome(payload)
        if payload.get("operation_outcome") == "success":
            assert outcome == "ok", f"{session} seq {sequence}: success read as {outcome!r}"
        if payload.get("invocation_status") == "pending":
            pending += 1
            assert outcome == "pending", f"{session} seq {sequence}: pending read as {outcome!r}"
            assert observation_summary(tool, payload) is not None

    assert pending == (1 if session == "ignite-d2r3" else 0)


@pytest.mark.parametrize("session", SESSIONS)
def test_the_lines_a_reader_will_actually_see(session):
    """Exact values, copied out of the ledger, one per tool at least."""

    calls = {
        (session, sequence): call_summary(tool, params)
        for sequence, tool, params in _calls(session)
    }
    for key, expected in REAL_CALL_LINES.items():
        if key[0] == session:
            assert calls[key] == expected, f"seq {key[1]}: {calls[key]!r} != {expected!r}"

    observations = {
        (session, sequence): observation_summary(tool, result)
        for sequence, tool, result in _results(session)
    }
    for key, expected in REAL_OBSERVATION_LINES.items():
        if key[0] == session:
            assert (
                observations[key] == expected
            ), f"seq {key[1]}: {observations[key]!r} != {expected!r}"


def test_every_tool_these_sessions_exercise_has_an_exact_line():
    """The exact-value table covers every tool the three sessions call."""

    covered = set()
    for session in SESSIONS:
        by_sequence = {sequence: tool for sequence, tool, _ in _calls(session)}
        for key in REAL_CALL_LINES:
            if key[0] == session:
                covered.add(by_sequence[key[1]])

    exercised = set()
    for session in SESSIONS:
        exercised |= set(ENVELOPES_PER_TOOL[session])
    assert covered == exercised
    assert exercised == {"advisor", "bash", "build", "phase", "project", "report", "search"}


def test_what_these_sessions_do_not_exercise():
    """Stated rather than skipped silently, so the gap is visible.

    `file_io` is the most-called tool in the wider corpus and no archived
    session here makes a single `file_io` call, so its branch is held only by
    `tests/test_trajectory_summaries.py`. No session records a refusal either,
    so `refusal_summary` has no real payload behind it.
    """
    tools = set()
    refusals = 0
    for session in SESSIONS:
        tools |= {tool for _, tool, _ in _calls(session)}
        refusals += sum(1 for event in _events(session) if event.get("kind") == "refusal_record")

    assert "file_io" not in tools
    assert refusals == 0
