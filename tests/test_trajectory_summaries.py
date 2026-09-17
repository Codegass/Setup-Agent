"""A turn reads as a line: what was asked, and what came back.

Every payload in this file is shaped like a payload the engine really writes.
Where a fixture here once carried an invented key, it was replaced with the real
one; `tests/test_trajectory_summaries_corpus.py` holds the derivation to three
archived sessions so an invented shape cannot pass again.
"""

from sag.trajectory.schema import SUMMARY_MAX_CHARS
from sag.trajectory.summaries import (
    call_summary,
    observation_outcome,
    observation_summary,
    refusal_summary,
)


def test_build_call_names_its_action_and_command():
    assert (
        call_summary(
            "build",
            {
                "command": "mvn clean verify",
                "system": "maven",
                "working_directory": "/workspace/commons-cli",
            },
        )
        == "verify mvn clean verify"
    )
    # The reviewed runner command is carried under `source_command` instead when
    # the envelope states a verb and its args separately.
    assert (
        call_summary(
            "build",
            {
                "action": "verify",
                "args": "-B clean",
                "source_command": "mvn -B clean verify",
                "system": "maven",
            },
        )
        == "verify mvn -B clean verify"
    )


def test_build_call_without_a_recognisable_action_shows_the_command():
    assert call_summary("build", {"command": "./gradlew check"}) == "./gradlew check"


def test_a_build_call_that_carries_only_a_verb_says_the_verb():
    """The commonest real build envelope has no `command` at all — just `action`."""

    assert (
        call_summary("build", {"action": "compile", "timeout": 1200, "working_directory": "/w"})
        == "compile"
    )
    assert call_summary("build", {"action": "test", "working_directory": "/w"}) == "test"


def test_a_build_call_carries_the_args_the_verb_was_given():
    """`test` and `test --no-daemon :clients:test` are different runs.

    The args name the coordinate the run actually drove, so a verb without them
    is a line that cannot be told apart from the run next to it.
    """
    assert (
        call_summary(
            "build",
            {
                "action": "test",
                "args": "--no-daemon :clients:test",
                "timeout": 1200,
                "working_directory": "/workspace/kafka",
            },
        )
        == "test --no-daemon :clients:test"
    )
    assert (
        call_summary("build", {"action": "compile", "args": "--no-daemon"}) == "compile --no-daemon"
    )
    # A command already spells out its own args; they are not appended twice.
    assert (
        call_summary(
            "build",
            {"action": "verify", "args": "-B clean", "source_command": "mvn -B clean verify"},
        )
        == "verify mvn -B clean verify"
    )


def test_build_args_that_are_not_one_line_of_text_are_left_out():
    """Only text is rendered, so a container never reaches the line raw.

    Every real envelope states `args` as a string, and the build tool's own
    parameter schema declares it one (`build_tool.py`, `"args": {"type":
    "string"}`). Anything else is dropped and the verb stands alone — which is
    the same protection a join would give, without a branch nothing reaches.
    """
    assert call_summary("build", {"action": "test", "args": None}) == "test"
    assert call_summary("build", {"action": "test", "args": "   "}) == "test"
    assert call_summary("build", {"action": "test", "args": {"skip": True}}) == "test"
    assert call_summary("build", {"action": "test", "args": ["-B", "clean"]}) == "test"


def test_the_mirrored_build_verbs_are_the_build_tool_s_own():
    """`_BUILD_ACTIONS` is a copy of a vocabulary this layer must not import.

    The derivation is read-only and must not import a Docker-bound tool, so the
    verbs are mirrored. A test may import it; a mirror that drifts is a word
    dropped off every build line it used to name.
    """
    from sag.tools.build import build_tool
    from sag.trajectory.summaries import _BUILD_ACTIONS

    assert _BUILD_ACTIONS == set(build_tool._ACTIONS)


def test_bash_call_collapses_the_command_to_its_first_line():
    assert call_summary("bash", {"command": "cat <<'EOF'\nline two\nEOF"}) == "cat <<'EOF'"


def test_project_clone_names_the_repository_and_ref():
    assert (
        call_summary(
            "project",
            {
                "action": "clone",
                "repo_url": "https://github.com/apache/commons-cli.git",
                "ref": "e17111798da51037659b3594d9c0b3b525040081",
            },
        )
        == "clone apache/commons-cli@e171117"
    )


def test_a_clone_ref_that_is_a_tag_is_carried_whole():
    """Real refs are tags, and half a tag is a ref that does not exist.

    Only a full 40-character hex sha is shortened. Every other ref is printed as
    the record holds it, or the reader is handed `release` for
    `releases/lucene/10.4.0` and `clone apache/ignite` for a pinned `2.18.0`.
    """
    assert (
        call_summary(
            "project",
            {
                "action": "clone",
                "repo_url": "https://github.com/apache/ignite.git",
                "ref": "2.18.0",
            },
        )
        == "clone apache/ignite@2.18.0"
    )
    assert (
        call_summary(
            "project",
            {
                "action": "clone",
                "repo_url": "https://github.com/apache/lucene.git",
                "ref": "releases/lucene/10.4.0",
            },
        )
        == "clone apache/lucene@releases/lucene/10.4.0"
    )


def test_only_a_full_length_sha_is_shortened():
    """A tag can be hex too, and `20250101` shortened to `2025010` names nothing.

    Forty characters exactly, hex only. Anything shorter or longer is a ref and
    is carried whole.
    """
    forty = "e17111798da51037659b3594d9c0b3b525040081"
    hexish_tag = "20250101"
    assert (
        call_summary(
            "project", {"action": "clone", "repo_url": "https://x/a/b.git", "ref": hexish_tag}
        )
        == "clone a/b@20250101"
    )
    assert (
        call_summary(
            "project", {"action": "clone", "repo_url": "https://x/a/b.git", "ref": forty[:39]}
        )
        == f"clone a/b@{forty[:39]}"
    )
    assert (
        call_summary("project", {"action": "clone", "repo_url": "https://x/a/b.git", "ref": forty})
        == "clone a/b@e171117"
    )


def test_project_provision_names_the_toolchain():
    assert (
        call_summary(
            "project", {"action": "provision", "java_distribution": "openjdk", "java_version": "17"}
        )
        == "provision openjdk 17"
    )
    assert (
        call_summary("project", {"action": "provision", "maven_version": "3.9"})
        == "provision maven 3.9"
    )


def test_a_provision_that_named_no_distribution_does_not_invent_one():
    """The real envelope is `{"action": "provision", "java_version": "17"}`.

    `java` is the name of the key the number came from. A distribution word
    would be a fact the payload never carried.
    """
    assert call_summary("project", {"action": "provision", "java_version": "17"}) == (
        "provision java 17"
    )


def test_file_io_and_search_and_phase_and_advisor_and_report():
    assert call_summary("file_io", {"action": "read", "path": "/workspace/x/pom.xml"}) == (
        "read /workspace/x/pom.xml"
    )
    assert call_summary("search", {"target": "output_abc", "pattern": "ERROR"}) == (
        "output_abc /ERROR/"
    )
    assert call_summary("search", {"target": "name:/workspace/kafka"}) == "name:/workspace/kafka"
    assert call_summary("phase", {"action": "done", "outcome": "success"}) == "done success"
    assert call_summary("advisor", {}) == "consult"
    assert call_summary("report", {"action": "generate"}) == "generate"
    assert call_summary("report", {}) == "generate"
    # `generate` is the only action the corpus carries today, so the line the
    # envelope states and the fallback agree. The rule is that the envelope
    # wins, and only a second word can say so.
    assert call_summary("report", {"action": "finalize"}) == "finalize"


def test_a_file_io_call_is_not_summarised_by_its_line_numbers():
    """`start_line` and `end_line` ride along on every real read.

    Falling through to the parameter-scraping tail promotes them ahead of the
    path and then clips the path away behind them.
    """
    summary = call_summary(
        "file_io",
        {
            "action": "read",
            "path": "/workspace/seatunnel-web/pom.xml",
            "start_line": 1,
            "end_line": 260,
        },
    )
    assert summary == "read /workspace/seatunnel-web/pom.xml"


def test_a_phase_call_that_stated_no_outcome_says_only_what_it_did():
    assert call_summary("phase", {"action": "note", "text": "Analyze facts: root build"}) == "note"
    assert call_summary("phase", {"action": "blocked", "outcome": "failed"}) == "blocked failed"


def test_an_unknown_tool_shows_its_first_three_scalar_parameters():
    summary = call_summary("mystery", {"a": 1, "b": "two", "c": True, "d": [1, 2], "e": "five"})
    assert summary == "a=1 b=two c=True"


def test_a_summary_is_truncated_with_an_ellipsis():
    summary = call_summary("bash", {"command": "echo " + "x" * 200})
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert summary.endswith("…")


def test_a_summary_that_collapses_to_nothing_is_none_not_a_blank():
    """ "Absence is stated, never implied" — and `""` implies a blank answer."""

    assert call_summary("file_io", {"action": "  ", "path": "\t"}) is None
    assert call_summary("bash", {"command": "   \n  "}) is None
    assert observation_summary("phase", {"metadata": {"gate_result": {"code": "  "}}}) is None


def test_the_clip_every_line_passes_through_never_returns_a_blank():
    """The last gate before a line reaches a field that forbids an empty string.

    Its callers all filter blanks before it, so this is the guard's only direct
    test — and without it the module can start answering an empty string where
    it means `None`, which is the distinction this layer is built on.
    """
    from sag.trajectory.summaries import _clip

    assert _clip("   ") is None
    assert _clip("\n\t ") is None
    assert _clip(None) is None
    assert _clip("  kept  ") == "kept"


def test_no_params_yields_no_summary():
    assert call_summary("build", None) is None
    assert call_summary("build", {}) is None


def test_the_two_tools_whose_summary_is_their_name_answer_without_params():
    """A deliberate asymmetry: `consult` and `generate` are read off the tool."""

    assert call_summary("advisor", None) == "consult"
    assert call_summary("report", None) == "generate"


def test_outcome_reads_success_as_ok():
    assert observation_outcome({"operation_outcome": "success"}) == "ok"


def test_outcome_reads_a_running_dispatch_as_pending():
    assert observation_outcome({"invocation_status": "pending"}) == "pending"
    assert (
        observation_outcome({"invocation_status": "pending", "operation_outcome": "unknown"})
        == "pending"
    )


def test_an_outcome_the_payload_declined_to_state_is_not_a_verdict():
    """`unknown` is the engine saying "I do not know"; `failed` would be a verdict.

    `partial` and `skipped` are legal words too, and neither of them is a
    failure. Where the record states no outcome this layer states none either.
    """
    assert observation_outcome({"operation_outcome": "failed"}) == "failed"
    assert observation_outcome({"operation_outcome": "unknown"}) is None
    assert observation_outcome({"operation_outcome": "partial"}) is None
    assert observation_outcome({"operation_outcome": "skipped"}) is None
    assert observation_outcome({}) is None


def test_a_cancelled_call_is_cancelled_and_a_crashed_one_failed():
    assert observation_outcome({"invocation_status": "cancelled"}) == "cancelled"
    assert observation_outcome({"invocation_status": "crashed"}) == "failed"
    assert observation_outcome({"invocation_status": "timeout"}) == "failed"


def test_every_word_the_engine_can_write_has_one_answer():
    """The whole mapping, pinned against the engine's two enums.

    `dispatched`, `running` and `polling` — the words this module used to look
    for — are not among them, which is why all 76 real pending calls read as
    failures before this table was written down.
    """
    from sag.evidence import InvocationStatus, OperationOutcome

    assert {
        status.value: observation_outcome({"invocation_status": status.value})
        for status in InvocationStatus
    } == {
        "pending": "pending",
        "completed": None,
        "timeout": "failed",
        "crashed": "failed",
        "cancelled": "cancelled",
    }
    assert {
        outcome.value: observation_outcome({"operation_outcome": outcome.value})
        for outcome in OperationOutcome
    } == {
        "unknown": None,
        "success": "ok",
        "partial": None,
        "failed": "failed",
        "skipped": None,
    }


def test_no_result_yields_no_outcome():
    assert observation_outcome(None) is None


def test_build_result_summarises_exit_tests_and_artifacts():
    result = {
        "invocation_status": "completed",
        "operation_outcome": "success",
        "facts": {"executed": 994, "passed": 933, "failed": 0, "skipped": 61},
        "metadata": {
            "exit_code": 0,
            "analysis": {"exit_code": 0, "artifacts_created": ["a.jar", "b.jar"]},
        },
    }
    assert observation_summary("build", result) == "exit 0 · 994 tests · 0 F · 61 S · 2 artifacts"


def test_the_error_count_comes_from_the_log_analysis():
    """`metadata.analysis.test_error_count` is the only error count a build takes.

    An error is not a failure: a test that could not run at all is counted
    apart from one that ran and failed, and a reader chasing 7 E is chasing
    something different from 1 F.
    """
    result = {
        "operation_outcome": "success",
        "facts": {"executed": 100, "failed": 2, "skipped": 3},
        "metadata": {"exit_code": 0, "analysis": {"test_error_count": 4}},
    }
    assert observation_summary("build", result) == "exit 0 · 100 tests · 2 F · 4 E · 3 S"


def test_the_exit_code_the_runner_reported_wins_over_the_log_reading():
    """Both keys are real and they agree in every record, so the rule needs saying.

    `metadata.exit_code` is what the dispatcher saw the process return;
    `metadata.analysis.exit_code` is what reading the log concluded. Only a
    constructed disagreement can state which one this line follows.
    """
    both = {
        "operation_outcome": "success",
        "metadata": {"exit_code": 0, "analysis": {"exit_code": 9}},
    }
    assert observation_summary("build", both) == "exit 0"

    log_only = {"operation_outcome": "success", "metadata": {"analysis": {"exit_code": 9}}}
    assert observation_summary("build", log_only) == "exit 9"


def test_a_five_figure_test_count_is_grouped_for_reading():
    result = {
        "operation_outcome": "success",
        "facts": {"executed": 18421, "failed": 3, "skipped": 31},
        "metadata": {"exit_code": 0},
    }
    assert observation_summary("build", result) == "exit 0 · 18,421 tests · 3 F · 31 S"


def test_a_count_the_payload_never_took_is_not_printed_as_zero():
    """Every term is stated only when its own number was taken.

    `facts["errors"]` does not exist — the error count lives in
    `metadata.analysis.test_error_count` — so the `0 E` this line used to print
    came from a `.get(..., 0)` over a key that was never there. `failed` and
    `skipped` are the same: a `None` must never render as `0 F`, and never as
    `None F`.
    """
    counted = {
        "operation_outcome": "success",
        "facts": {"executed": 994, "passed": 994, "failed": None, "skipped": None},
        "metadata": {"exit_code": 0},
    }
    assert observation_summary("build", counted) == "exit 0 · 994 tests"

    uncounted = {"operation_outcome": "success", "facts": {}, "metadata": {"exit_code": 0}}
    assert observation_summary("build", uncounted) == "exit 0"

    # `True` is an `int` in Python, and it is not a count of anything.
    flagged = {
        "operation_outcome": "success",
        "facts": {"executed": 5, "failed": True, "skipped": False},
        "metadata": {"exit_code": 0, "analysis": {"test_error_count": True}},
    }
    assert observation_summary("build", flagged) == "exit 0 · 5 tests"

    partial = {
        "operation_outcome": "success",
        "facts": {"executed": 994, "failed": 3},
        "metadata": {"exit_code": 0},
    }
    assert observation_summary("build", partial) == "exit 0 · 994 tests · 3 F"


def test_failed_build_result_names_its_error():
    result = {
        "operation_outcome": "failed",
        "error_code": "MAVEN_VERSION_BELOW_MINIMUM",
        "metadata": {"analysis": {"exit_code": 1}},
    }
    assert observation_summary("build", result) == "exit 1 · MAVEN_VERSION_BELOW_MINIMUM"


def test_a_failed_build_that_states_only_a_log_diagnosis_names_that():
    result = {
        "operation_outcome": "failed",
        "metadata": {"exit_code": 1, "analysis": {"exit_code": 1, "error_type": "MODULE_BANNED"}},
    }
    assert observation_summary("build", result) == "exit 1 · MODULE_BANNED"


def test_a_failed_test_run_is_counted_first_and_then_says_why():
    """A build that ran 2,692 tests and failed still ran 2,692 tests.

    The reason is appended, not substituted: the counts are the most
    informative thing in the record, and a failing run is exactly when a reader
    needs them.
    """
    result = {
        "operation_outcome": "failed",
        "error_code": "TEST_FAILURE",
        "facts": {"executed": 2692, "failed": 1, "skipped": 13},
        "metadata": {"exit_code": 1, "analysis": {"test_error_count": 7}},
    }
    assert observation_summary("build", result) == (
        "exit 1 · 2,692 tests · 1 F · 7 E · 13 S · TEST_FAILURE"
    )


def test_provision_result_names_the_version_it_verified():
    result = {
        "operation_outcome": "success",
        "metadata": {"verified_java_version": "17.0.20", "java_version": "17"},
    }
    assert observation_summary("project", result) == "java 17.0.20"
    assert (
        observation_summary(
            "project", {"operation_outcome": "success", "metadata": {"java_version": "17"}}
        )
        == "java 17"
    )


def test_clone_result_names_the_commit_and_path():
    result = {
        "operation_outcome": "success",
        "metadata": {
            "resolved_commit": "e17111798da51037659b3594d9c0b3b525040081",
            "clone_path": "/workspace/commons-cli",
            "ref": "2.18.0",
        },
    }
    assert observation_summary("project", result) == "e171117 → /workspace/commons-cli"


def test_a_failed_project_call_never_reads_like_a_successful_one():
    """Success-shaped metadata survives a failure; the outcome decides first."""

    result = {
        "operation_outcome": "failed",
        "error_code": "ENV_RUNTIME_REQUIREMENT_MISMATCH",
        "metadata": {"verified_java_version": "17.0.20"},
    }
    assert observation_summary("project", result) == "ENV_RUNTIME_REQUIREMENT_MISMATCH"


def test_phase_result_names_the_gate_word_and_reason():
    """The gate word is `metadata.gate_result["code"]`, and it is a dict."""

    result = {
        "operation_outcome": "success",
        "facts": {"phase": "provision"},
        "metadata": {
            "gate_result": {
                "accepted": True,
                "code": "workspace_present",
                "reason": "workspace /workspace/commons-cli exists",
                "validator_state": "green",
            }
        },
    }
    # The reason code, bare. It used to be labelled `gate <code>`, which put
    # the word `gate` over two different facts once the turn stream rendered
    # the word a gate DELIVERED beside it.
    assert observation_summary("phase", result) == (
        "workspace_present · workspace /workspace/commons-cli exists"
    )


def test_a_phase_result_with_no_gate_says_the_signal_it_has():
    result = {"operation_outcome": "success", "metadata": {"phase_signal": "note"}}
    assert observation_summary("phase", result) == "note"
    assert observation_summary("phase", {"operation_outcome": "success", "metadata": {}}) is None


def test_advisor_result_says_advice_was_delivered():
    assert observation_summary("advisor", {"operation_outcome": "success"}) == "advice delivered"


def test_a_pending_job_names_its_handle():
    result = {
        "invocation_status": "pending",
        "operation_outcome": "unknown",
        "metadata": {"job_id": "2c4d56b2fdca"},
    }
    assert observation_summary("build", result) == "running · job 2c4d56b2fdca"
    assert observation_summary("build", {"invocation_status": "pending"}) == "running"


def test_a_search_result_says_whether_it_matched():
    """`facts["matched"]` is a yes/no, not a count.

    `search_tool.py` writes it as `True`, `False`, `bool(lines)` or `None`, so
    the honest line is the answer to "did the pattern hit", and a number here
    would be a count nobody took.
    """
    hit = {"operation_outcome": "success", "facts": {"matched": True, "target": "file:/w/pom.xml"}}
    assert observation_summary("search", hit) == "matched"

    miss = {"operation_outcome": "success", "facts": {"matched": False}}
    assert observation_summary("search", miss) == "no match"

    silent = {"operation_outcome": "success", "facts": {"matched": None}}
    assert observation_summary("search", silent) is None
    assert observation_summary("search", {"operation_outcome": "success"}) is None


def test_a_search_result_that_counted_its_matches_says_the_count():
    """`metadata.total_matches` is a real count, and it is preferred when stated."""

    counted = {
        "operation_outcome": "success",
        "metadata": {"total_matches": 80, "matches_shown": 80},
    }
    assert observation_summary("search", counted) == "80 matches"

    many = {"operation_outcome": "success", "metadata": {"total_matches": 12345}}
    assert observation_summary("search", many) == "12,345 matches"

    # A `True` is not a count of one.
    flagged = {
        "operation_outcome": "success",
        "facts": {"matched": False},
        "metadata": {"total_matches": True},
    }
    assert observation_summary("search", flagged) == "no match"


def test_a_failed_search_names_its_error_instead():
    failed = {"operation_outcome": "failed", "error_code": "SEARCH_FAILED", "facts": {}}
    assert observation_summary("search", failed) == "SEARCH_FAILED"


def test_a_result_that_states_no_error_states_nothing():
    """The generic tail carries a code or says nothing; `failed` is the outcome's word."""

    assert observation_summary("bash", {"operation_outcome": "failed"}) is None
    assert (
        observation_summary("bash", {"operation_outcome": "failed", "error_code": "COMMAND_FAILED"})
        == "COMMAND_FAILED"
    )
    assert observation_summary("bash", {"operation_outcome": "success"}) is None


def test_a_refusal_reads_as_the_code_the_record_carries():
    """A refusal record holds a code and nothing else to say.

    `RefusalRecordPayload` is strict and its fields are `tool`, `tool_call_id`,
    `refusal_code`, `exact_params_sha256` and `repair_intent` — there is no
    `reason`, so no ledger written today can produce one. These three are the
    whole of what a reader can be shown.
    """
    assert refusal_summary({"refusal_code": "CALL_NOT_EXECUTED"}) == ("cancelled", "cancelled")
    assert refusal_summary({"refusal_code": "PHASE_ACTION_MISMATCH"}) == (
        "refused",
        "PHASE_ACTION_MISMATCH",
    )
    # Real codes are not all SHOUTED.
    assert refusal_summary({"refusal_code": "execution_refused:evidence_closed"}) == (
        "refused",
        "execution_refused:evidence_closed",
    )


def test_a_refusal_reason_is_carried_if_a_later_record_ever_states_one():
    """Nothing emits this today; the branch is kept for a ledger that might.

    Stated here rather than left looking like live behaviour — no
    `refusal_record` in any archive carries a `reason` field, and the payload
    model forbids one.
    """
    assert refusal_summary({"refusal_code": "CALL_NOT_EXECUTED", "reason": "phase closed"}) == (
        "cancelled",
        "cancelled: phase closed",
    )
    assert refusal_summary({"refusal_code": "REPAIR_INTENT_REQUIRED", "reason": "no intent"}) == (
        "refused",
        "REPAIR_INTENT_REQUIRED · no intent",
    )
    assert refusal_summary({"refusal_code": "skipped"}) == ("refused", "skipped")


def test_the_cancellation_code_is_the_engine_s_own():
    from sag.agent.control_events import CANCELLED_CALL_REFUSAL_CODE

    outcome, _ = refusal_summary({"refusal_code": CANCELLED_CALL_REFUSAL_CODE})
    assert outcome == "cancelled"
