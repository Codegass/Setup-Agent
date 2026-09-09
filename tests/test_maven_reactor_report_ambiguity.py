"""Maven history preserves ambiguous rows without publishing a module score."""

import json

import pytest

from sag.tools.report_tool import ReportTool


def _entry(rows, *, blocks=None, ambiguous=()):
    return {
        "event": "build_summary",
        "working_directory": "/workspace/proj",
        "command": "mvn package",
        "reactor_summary": [{"module": name, "status": status} for name, status in rows],
        "reactor_summary_blocks": blocks if blocks is not None else [],
        "reactor_ambiguous_labels": list(ambiguous),
    }


def _block(count, **overrides):
    return {
        "start_line": 1,
        "end_line": count + 3,
        "row_count": count,
        "result": "SUCCESS",
        "duplicate_labels": [],
        "unparsed_lines": [],
        **overrides,
    }


def _history(*entries):
    class Orchestrator:
        def execute_command(self, command, **_kwargs):
            return {
                "exit_code": 0,
                "output": "\n".join(json.dumps(entry) for entry in entries),
            }

    return ReportTool(docker_orchestrator=Orchestrator())._load_test_history()


def test_same_entry_failure_is_not_overwritten_by_an_equal_success_label():
    entry = _entry(
        [("Camel :: Common", "failure"), ("Camel :: Common", "success")],
        blocks=[_block(2, duplicate_labels=["Camel :: Common"])],
        ambiguous=["Camel :: Common"],
    )

    history = _history(entry)

    assert history["reactor_records"] == entry["reactor_summary"]
    assert (
        history["reactor_entries"][0]["reactor_summary_blocks"] == entry["reactor_summary_blocks"]
    )
    assert history["reactor_entries"][0]["reactor_ambiguous_labels"] == ["Camel :: Common"]
    assert ReportTool()._reactor_status_from_history(history) == {}


@pytest.mark.parametrize(
    "entry",
    [
        _entry([("same", "failure"), ("same", "success")]),
        _entry([("m-0", "failure"), ("m0", "success")]),
        _entry([("a", "success"), ("b", "success")], blocks=[_block(1), _block(1)]),
        _entry([("a", "success")], blocks=[_block(1, result=None)]),
        _entry([], blocks=[_block(0, unparsed_lines=[2])]),
    ],
)
def test_ambiguous_history_cannot_fall_back_to_a_successful_filesystem_scan(entry):
    class Validator:
        def scan_modules(self, *_args):
            pytest.fail("Ambiguous reactor evidence must not become a filesystem module score")

    tool = ReportTool()
    tool.physical_validator = Validator()
    history = _history(entry)

    metrics = tool._build_module_metrics(history, generated_at="t")

    assert metrics["status"] == "unavailable"
    assert metrics["module_summary"] is None
    assert metrics["modules"] == []
    assert metrics["reactor_entries"] == history["reactor_entries"]
    assert metrics["reactor_records"] == entry["reactor_summary"]
    markdown = "\n".join(tool._render_submodule_breakdown(metrics))
    assert "Module scope unavailable" in markdown
    assert "built /" not in markdown


def test_independent_history_entries_keep_a_rebuild_distinct_from_duplicate_labels():
    history = _history(
        _entry([("core", "failure")], blocks=[_block(1, result="FAILURE")]),
        _entry([("core", "success")], blocks=[_block(1)]),
    )

    assert len(history["reactor_entries"]) == 2
    assert ReportTool()._reactor_status_from_history(history) == {"core": "success"}


def test_flat_legacy_history_with_duplicate_names_is_not_assumed_to_be_a_retry():
    history = {
        "reactor_records": [
            {"module": "core", "status": "failure"},
            {"module": "core", "status": "success"},
        ]
    }

    assert ReportTool()._reactor_status_from_history(history) == {}


def test_module_summary_dashboard_discloses_unavailable_scope_without_a_zero_score():
    tool = ReportTool()
    snapshot = {"status": {"modules_unavailable_reason": "Maven module labels are ambiguous"}}

    markdown = "\n".join(tool._render_summary_dashboard(snapshot))

    assert "Module Coverage" in markdown
    assert "unavailable" in markdown
    assert "0/0" not in markdown


def test_unavailable_module_artifact_does_not_become_a_zero_web_rollup():
    from sag.web.session_registry import _module_rollup_from_metrics, _modules_payload_from_metrics

    history = _history(_entry([("same", "failure"), ("same", "success")]))
    metrics = ReportTool()._build_module_metrics(history, generated_at="t")
    persisted = json.loads(json.dumps(metrics))

    assert _module_rollup_from_metrics(persisted) is None
    assert _modules_payload_from_metrics(persisted) == []
    assert persisted["reactor_records"][0]["status"] == "failure"


def test_shared_parser_scope_issues_survive_history_and_withhold_the_module_score():
    entry = _entry([])
    entry["reactor_scope_issues"] = ["invocation_boundaries", "project_count_mismatch"]

    history = _history(entry)
    metrics = ReportTool()._build_module_metrics(history, generated_at="t")

    assert history["reactor_entries"][0]["reactor_scope_issues"] == entry["reactor_scope_issues"]
    assert metrics["status"] == "unavailable"
    assert metrics["module_summary"] is None
    assert "invocation_boundaries" in metrics["reason"]
    assert "project_count_mismatch" in metrics["reason"]
