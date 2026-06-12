"""One verdict everywhere (spec §6): failed < partial < success; the run's
surfaced verdict is the MINIMUM of machine outcome, physical verdict, and
the conflict cap. Round-5 iceberg: CLI said success while the report said
PARTIAL — structurally impossible once both read this kernel."""

from sag.verdict import VERDICT_ORDER, combine_verdicts, run_verdict


def test_order():
    assert VERDICT_ORDER == ["failed", "partial", "success"]


def test_combine_takes_minimum():
    assert combine_verdicts("success", "partial") == "partial"
    assert combine_verdicts("partial", "failed") == "failed"
    assert combine_verdicts("success", "success") == "success"


def test_combine_ignores_none_and_unknown():
    assert combine_verdicts("success", None) == "success"
    assert combine_verdicts(None, None) == "success"  # no objections raised
    assert combine_verdicts("unknown", "success") == "success"


def test_run_verdict_conflicts_cap_at_partial():
    v = run_verdict(machine_outcome="success", physical_verdict="success",
                    conflicts=["test_report_parse_error"])
    assert v == "partial"


def test_run_verdict_machine_failure_dominates():
    v = run_verdict(machine_outcome="failed", physical_verdict="success", conflicts=[])
    assert v == "failed"


def test_run_verdict_clean_success():
    assert run_verdict("success", "success", []) == "success"
