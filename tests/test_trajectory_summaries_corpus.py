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
#: result may honestly have nothing to state — a bash that exited 0 carries no
#: line of its own — so these are held exactly, not as totals. `phase`, `build`
#: and `search` at full count are the branches that summarised nothing before.
OBSERVATIONS_NAMED_PER_TOOL = {
    "camel-quarkus-d2r3": {
        "advisor": 2,
        "bash": 6,
        "build": 2,
        "phase": 14,
        "project": 6,
        "search": 21,
    },
    "ignite-d2r3": {"advisor": 3, "bash": 1, "build": 2, "phase": 6, "project": 3, "search": 17},
    "kafka-d2r3": {"advisor": 2, "build": 2, "phase": 7, "project": 3, "search": 7},
}

#: `(session, control sequence) -> the line a reader is shown`. Copied out of
#: the archived ledgers, so each one states a real command, a real ref, a real
#: gate word, a real job handle.
REAL_CALL_LINES = {
    ("camel-quarkus-d2r3", 3): "clone apache/camel-quarkus@3.36.0",
    ("camel-quarkus-d2r3", 11): "done success",
    ("camel-quarkus-d2r3", 61): "consult",
    ("camel-quarkus-d2r3", 63): "compile",
    # The args name the coordinate the run drove; the verb alone would not.
    ("camel-quarkus-d2r3", 89): "test -DskipITs -DskipIntegrationTests -DskipNativeTests",
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
    ("kafka-d2r3", 119): "compile --no-daemon",
    ("kafka-d2r3", 141): "test --no-daemon :clients:test",
}

REAL_OBSERVATION_LINES = {
    ("camel-quarkus-d2r3", 4): "5dec869 → /workspace/camel-quarkus",
    (
        "camel-quarkus-d2r3",
        12,
    ): "workspace_present · workspace /workspace/camel-quarkus exists",
    ("camel-quarkus-d2r3", 62): "advice delivered",
    ("camel-quarkus-d2r3", 108): "COMMAND_FAILED",
    ("camel-quarkus-d2r3", 70): "exit 1 · JAVA_VERSION_ERROR",
    ("camel-quarkus-d2r3", 99): "ENV_MAVEN_EXECUTABLE_NAME_MISMATCH",
    ("camel-quarkus-d2r3", 54): "matched",
    ("ignite-d2r3", 18): "SEARCH_FAILED",
    ("ignite-d2r3", 145): "no match",
    # The two results in these sessions that counted their matches.
    ("ignite-d2r3", 177): "80 matches",
    ("ignite-d2r3", 157): "note",
    ("ignite-d2r3", 174): "exit 0 · 5 artifacts",
    # The one dispatched job in the three sessions: pending, not failed.
    ("ignite-d2r3", 238): "running · job 2c4d56b2fdca",
    ("kafka-d2r3", 4): "26b251a → /workspace/kafka",
    ("kafka-d2r3", 124): "exit 0",
    ("kafka-d2r3", 146): "80 matches",
    ("kafka-d2r3", 143): "exit 1 · DETACHED_OPERATION_FAILED",
}


#: One real build result, verbatim: the `tool_result` at sequence 116 of
#: `logs/advisor-high20-mini-high-httpcomponents-client-20260914/runs/
#: httpcomponents-client/logs/session_20260914_044129_085078_dbb0f0d5b4b9_60849/
#: control_events.jsonl`. Copied out of that ledger by machine, not composed
#: here; the only edit is that the bulky text fields — `output`, `warnings`,
#: `reactor_summary`, `failed_tests` — are left out. Every key the summarisers
#: read is as the run wrote it.
#:
#: It is embedded because **no build result in any of the three archived
#: sessions carries `facts["executed"]`**, so the counts clause — the one clause
#: that produced the original `0 E` defect — had no real payload behind it at
#: all, and deleting it outright left this fence green.
REAL_FAILED_BUILD_RESULT = {
    "error_code": "TEST_FAILURE",
    "facts": {
        "action": "install",
        "effective_action": "install",
        "executed": 2692,
        "failed": 1,
        "pass_rate": 99.21991084695394,
        "passed": 2671,
        "requested_action": "install",
        "skipped": 13,
        "system": "maven",
    },
    "failure_signature": "TEST_FAILURE:c12b9a9cdb4a175c",
    "invocation_status": "completed",
    "metadata": {
        "analysis": {
            "artifacts_created": [
                "/workspace/httpcomponents-client/httpclient5/target/httpclient5-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5/target/httpclient5-5.7-alpha2-SNAPSHOT-tests.jar",
                "/workspace/httpcomponents-client/httpclient5/target/httpclient5-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5/target/httpclient5-5.7-alpha2-SNAPSHOT-tests.jar",
                "/workspace/httpcomponents-client/httpclient5-sse/target/httpclient5-sse-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-sse/target/httpclient5-sse-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-cache/target/httpclient5-cache-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-cache/target/httpclient5-cache-5.7-alpha2-SNAPSHOT-tests.jar",
                "/workspace/httpcomponents-client/httpclient5-cache/target/httpclient5-cache-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-cache/target/httpclient5-cache-5.7-alpha2-SNAPSHOT-tests.jar",
                "/workspace/httpcomponents-client/httpclient5-observation/target/httpclient5-observation-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-observation/target/httpclient5-observation-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-fluent/target/httpclient5-fluent-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-fluent/target/httpclient5-fluent-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-websocket/target/httpclient5-websocket-5.7-alpha2-SNAPSHOT.jar",
                "/workspace/httpcomponents-client/httpclient5-websocket/target/httpclient5-websocket-5.7-alpha2-SNAPSHOT.jar",
            ],
            "build_success": False,
            "error_type": "MODULE_FAILURE",
            "exit_code": 1,
            "test_error_count": 7,
            "test_failure_count": 1,
            "tests_run": {"errors": 7, "failures": 1, "skipped": 13, "total": 2692},
        },
        "command": "/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml clean verify "
        "install -P-use-toolchains,nodoclint",
        "error_type": "TEST_FAILURE",
        "exit_code": 1,
        "system": "maven",
        "working_directory": "/workspace/httpcomponents-client",
    },
    "operation_outcome": "failed",
}


#: Results that honestly state nothing. A bash that exited 0 and a report that
#: was written carry no line of their own, and silence is the answer there —
#: pinned by sequence so the generic tail cannot go back to inventing `failed`.
REAL_SILENT_OBSERVATIONS = {
    ("camel-quarkus-d2r3", 87),
    ("camel-quarkus-d2r3", 102),
    ("camel-quarkus-d2r3", 158),
    ("camel-quarkus-d2r3", 256),
    ("ignite-d2r3", 21),
    ("ignite-d2r3", 24),
    ("ignite-d2r3", 188),
    ("kafka-d2r3", 18),
    ("kafka-d2r3", 22),
    ("kafka-d2r3", 165),
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
        if summary is None:
            continue
        assert _is_a_line(summary), f"{session} seq {sequence}: {summary!r} is not one line"
        named[tool] = named.get(tool, 0) + 1

    assert total == ENVELOPES_PER_TOOL[session]
    # Held apart from the total above, so a tool that stops summarising fails
    # here rather than passing on a count it no longer earns.
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


def test_every_tool_that_says_something_has_an_exact_observation_line():
    """The counterpart of the call coverage test, for what came back.

    Without it the exact-value table can hold nothing for a whole tool while its
    coverage count still passes — which is how the generic tail could go back to
    inventing a word with this fence still green.
    """
    covered = set()
    said_something = set()
    for session in SESSIONS:
        by_sequence = {sequence: tool for sequence, tool, _ in _results(session)}
        for key in REAL_OBSERVATION_LINES:
            if key[0] == session:
                covered.add(by_sequence[key[1]])
        said_something |= set(OBSERVATIONS_NAMED_PER_TOOL[session])

    assert covered == said_something
    assert said_something == {"advisor", "bash", "build", "phase", "project", "search"}


@pytest.mark.parametrize("session", SESSIONS)
def test_the_results_that_honestly_state_nothing(session):
    """Silence is an answer, and it is pinned like any other."""

    silent = {
        (session, sequence)
        for sequence, tool, result in _results(session)
        if observation_summary(tool, result) is None
    }
    assert silent == {key for key in REAL_SILENT_OBSERVATIONS if key[0] == session}


def test_a_real_build_that_ran_tests_and_failed_is_counted_and_then_says_why():
    """The clause the original defect lived in, held to a record a run wrote.

    2,692 tests ran, 1 failed, 7 errored, 13 were skipped, 16 artifacts landed,
    and the build failed. Every one of those numbers is in the record; none of
    them was defaulted, and the reason is appended rather than swallowing them.
    """
    assert observation_outcome(REAL_FAILED_BUILD_RESULT) == "failed"
    line = observation_summary("build", REAL_FAILED_BUILD_RESULT)
    assert line == "exit 1 · 2,692 tests · 1 F · 7 E · 13 S · 16 artifacts · TEST_FAILURE"
    assert _is_a_line(line)


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
