"""One Maven grammar; rows are observations, not unique module coordinates."""

from collections import Counter
from pathlib import Path

import pytest

from sag.metrics.maven_reactor import parse_maven_reactor


@pytest.mark.parametrize("padding", ["", ". ", "............ "])
@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE", "SKIPPED"])
def test_versioned_names_and_zero_or_one_padding_dot(padding, status):
    text = (
        "2026-09-07T01:02:03Z [\x1b[34mINFO\x1b[m] Reactor Summary for x:\n"
        f"2026-09-07T01:02:03Z [INFO] RocketMQ 5.5.1 {padding}{status} [ 0.1 s]\n"
        "2026-09-07T01:02:03Z [INFO] BUILD FAILURE\n"
    )
    (block,) = parse_maven_reactor(text)
    assert [(row.module, row.status) for row in block.rows] == [("RocketMQ 5.5.1", status)]
    assert block.result == "FAILURE"
    assert block.rows[0].line_number == 2


def test_repeated_labels_and_summary_boundaries_are_preserved():
    text = (
        "[INFO] Reactor Summary:\n"
        "[INFO] Same :: Name ... SUCCESS\n"
        "[INFO] Same :: Name ... FAILURE\n"
        "[INFO] BUILD FAILURE\n"
        "[INFO] outside ... SUCCESS\n"
        "[INFO] Reactor Summary:\n"
        "[INFO] Same :: Name ... SKIPPED\n"
        "[INFO] BUILD FAILURE\n"
    )
    blocks = parse_maven_reactor(text)
    assert [[r.status for r in b.rows] for b in blocks] == [["SUCCESS", "FAILURE"], ["SKIPPED"]]
    assert blocks[0].duplicate_labels == ("Same :: Name",)
    assert blocks[1].duplicate_labels == ()


def test_no_header_is_not_a_reactor_and_truncated_rows_are_disclosed():
    assert parse_maven_reactor("[INFO] fake ... SUCCESS\n[INFO] BUILD SUCCESS") == ()
    (block,) = parse_maven_reactor(
        "[INFO] Reactor Summary:\n[INFO] good ... SUCCESS\n[INFO] cut ... SUCC"
    )
    assert block.result is None
    assert block.unparsed_lines == (3,)


def test_building_is_a_valid_word_in_a_reactor_display_name():
    (block,) = parse_maven_reactor(
        "[INFO] Reactor Summary:\n[INFO] Building Blocks ... SUCCESS\n"
        "[INFO] Scanning for projects ... SUCCESS\n[INFO] next ... SKIPPED\n"
        "[INFO] BUILD SUCCESS"
    )
    assert [r.module for r in block.rows] == ["Building Blocks", "Scanning for projects", "next"]


@pytest.mark.parametrize(
    "ending",
    [
        "",
        "\n[INFO] cut ... SUCC\n[INFO] BUILD SUCCESS",
        "\n[INFO] BUILD SUCCESS\n[INFO] Reactor Summary:\n[INFO] second ... SUCCESS\n[INFO] BUILD SUCCESS",
    ],
)
def test_unresolved_summary_boundaries_are_a_declared_receipt_omission(ending, monkeypatch):
    from types import SimpleNamespace

    from sag.tools.internal import maven_tool

    output = "[INFO] Reactor Summary:\n[INFO] first ... SUCCESS" + ending
    captured = {}
    monkeypatch.setattr(maven_tool, "snapshot_reports", lambda *args: {})

    def record(execute, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(maven_tool, "record_invocation", record)
    tool = maven_tool.MavenTool(SimpleNamespace(execute_command=lambda *_: {}))
    tool._record_invocation_receipt(
        requested_action="compile",
        effective_action="compile",
        argv="mvn compile",
        working_directory="/workspace",
        attempt=1,
        result={"full_output": output, "exit_code": 0},
        before={},
    )
    assert captured["module_outcomes"] == []
    assert captured["declared_omissions"][0]["field"] == "module_outcomes"
    assert captured["output"] == output
    # Diagnostic parsing remains lossless even though scope authority is withheld.
    assert maven_tool._reactor_module_outcomes(output)[0] == {
        "module": "first",
        "status": "success",
    }


def test_a_later_build_result_cannot_finish_an_interrupted_summary():
    blocks = parse_maven_reactor(
        "[INFO] Reactor Summary:\n[INFO] first ... SUCCESS\n"
        "[INFO] Scanning for projects...\n[INFO] Building another 1.0\n"
        "[INFO] BUILD SUCCESS\n"
    )
    assert blocks[0].result is None


@pytest.mark.parametrize("kind", ["prior_unfinished", "later_unfinished", "missing_row"])
def test_ci_and_local_receipts_refuse_the_same_proven_boundary_gaps(kind):
    from sag.metrics.ci_logs import modules_from_log
    from sag.tools.internal.maven_tool import MavenTool, _reactor_receipt_fields

    text = "[INFO] Reactor Summary:\n[INFO] first ... SUCCESS\n[INFO] second ... SUCCESS\n[INFO] BUILD SUCCESS"
    if kind == "prior_unfinished":
        text = (
            "[INFO] Scanning for projects...\n[INFO] Building previous\n[INFO] Scanning for projects...\n"
            + text
        )
    elif kind == "later_unfinished":
        text += "\n[INFO] Building unfinished 1.0"
    else:
        text = "\n".join(f"[INFO] Building m{i} [" + f"{i}/3]" for i in range(1, 4)) + "\n" + text
    assert modules_from_log(text).modules == ()
    assert _reactor_receipt_fields(text)["declared_omissions"]
    analysis = MavenTool(None)._analyze_maven_output(text, 0)
    assert analysis["reactor_scope_issues"]
    assert len(analysis["reactor_summary"]) == 2


@pytest.mark.parametrize(
    "project,counts",
    [("rocketmq", [19]), ("camel-examples", [83, 83]), ("camel", [687])],
)
def test_frozen_d3r1_rows_reach_both_maven_consumers(project, counts):
    from sag.tools.internal.maven_tool import MavenTool, _reactor_module_outcomes

    text = (
        Path(__file__).parent / "fixtures/d3r1_remediation/maven_reactor" / f"{project}.txt"
    ).read_text()
    blocks = parse_maven_reactor(text)
    assert [len(b.rows) for b in blocks] == counts
    assert all(b.result is not None and not b.unparsed_lines for b in blocks)
    rows = [r for b in blocks for r in b.rows]
    assert _reactor_module_outcomes(text) == [
        {"module": r.module, "status": r.status.lower()} for r in rows
    ]
    analysis = MavenTool(orchestrator=None)._analyze_maven_output(text, exit_code=0)
    assert [(r["module"], r["status"]) for r in analysis["reactor_summary"]] == [
        (r.module, r.status) for r in rows
    ]
    assert [b["row_count"] for b in analysis["reactor_summary_blocks"]] == counts
    if project == "camel":
        assert Counter(r.status for r in rows) == {"SUCCESS": 649, "FAILURE": 2, "SKIPPED": 36}
        assert analysis["reactor_ambiguous_labels"] == [
            "Camel :: Test :: Spring :: JUnit5",
            "Camel :: Integration Tests",
        ]
