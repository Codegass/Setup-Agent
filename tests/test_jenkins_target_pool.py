"""A Jenkins final-results pool cannot be replaced by aggregate log totals."""

from copy import deepcopy

import pytest

from scripts.d3_harvest_target import HarvestError, cell_from_jenkins_report

BUILD = "https://ci.example/job/one/225/"
LOG = "[INFO] Reactor Summary:\n[INFO] core ... SUCCESS\n[INFO] BUILD SUCCESS\nFinished: SUCCESS\n"


def report():
    return {
        "totalCount": 2,
        "failCount": 0,
        "skipCount": 0,
        "childReports": [
            {
                "child": {"url": "https://ci.example/job/one/g$core/225/"},
                "result": {
                    "passCount": 2,
                    "failCount": 0,
                    "skipCount": 0,
                    "suites": [
                        {
                            "cases": [
                                {
                                    "className": "a.A",
                                    "name": "repeated",
                                    "status": "PASSED",
                                    "skipped": False,
                                },
                                {
                                    "className": "a.A",
                                    "name": "repeated",
                                    "status": "PASSED",
                                    "skipped": False,
                                },
                            ]
                        }
                    ],
                },
            }
        ],
    }


def parse(payload, *, log=LOG):
    return cell_from_jenkins_report(
        payload,
        cell_id="Jenkins ubuntu JDK 17 #225",
        build_url=BUILD,
        console_text=log,
        command="mvn verify",
        evidence_refs=("testReport.json", "console.log"),
    )


def test_jenkins_final_case_occurrences_are_not_string_deduplicated():
    cell = parse(report()).cell
    assert cell.executed_count == len(cell.executed_ids) == 2
    assert cell.modules == ("core",) and cell.grade == "A"


@pytest.mark.parametrize(
    "damage", ["total", "child_count", "status", "build", "duplicate_child", "missing_cases"]
)
def test_partial_or_other_build_pool_is_rejected(damage):
    payload = report()
    result = payload["childReports"][0]["result"]
    if damage == "total":
        payload["totalCount"] = 3
    elif damage == "child_count":
        result["passCount"] = 1
    elif damage == "status":
        result["suites"][0]["cases"][0]["status"] = "UNKNOWN"
    elif damage == "build":
        payload["childReports"][0]["child"]["url"] = "https://ci.example/job/one/g$core/226/"
    elif damage == "duplicate_child":
        payload["childReports"].append(deepcopy(payload["childReports"][0]))
    else:
        result["suites"][0]["cases"].pop()
    with pytest.raises(HarvestError):
        parse(payload)


def test_success_badge_without_bounded_module_log_is_not_a_complete_target():
    with pytest.raises(HarvestError):
        parse(report(), log="Finished: SUCCESS")


def test_repeated_red_rows_are_final_executions_not_assumed_retries():
    payload = report()
    payload["failCount"] = 2
    result = payload["childReports"][0]["result"]
    result.update(passCount=0, failCount=2)
    for case in result["suites"][0]["cases"]:
        case["status"] = "FAILED"
    cell = parse(payload).cell
    assert cell.red_count == cell.executed_count == 2


def test_identity_bound_keeps_full_count_without_truncating_the_pool(monkeypatch):
    monkeypatch.setattr("scripts.d3_harvest_target.IDENTITY_COUNT_BOUND", 1)
    cell = parse(report()).cell
    assert cell.executed_count == 2 and cell.executed_ids == ()
