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


def test_run_verdict_ignores_conflicts_already_adjudicated_by_threshold():
    """test_failures_detected / test_errors_detected merely RESTATE the counted
    failures that the pass-rate threshold policy (evaluate_run_verdict) already
    accepted into the physical verdict. Feeding them back in double-adjudicates
    the same failures: a 206/214 (96.3% >= threshold) run was announced
    'partial' where pre-stage-3 said SUCCESS (round-6 review)."""
    assert run_verdict("success", "success",
                       ["test_failures_detected", "test_errors_detected"]) == "success"


def test_run_verdict_genuine_uncertainty_still_caps_alongside_adjudicated():
    assert run_verdict("success", "success",
                       ["test_failures_detected", "test_report_parse_error"]) == "partial"


def test_run_verdict_machine_failure_dominates():
    v = run_verdict(machine_outcome="failed", physical_verdict="success", conflicts=[])
    assert v == "failed"


def test_run_verdict_clean_success():
    assert run_verdict("success", "success", []) == "success"
