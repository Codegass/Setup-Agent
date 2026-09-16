from sag.web.demo_data import build_demo_dashboard, get_demo_session


def test_demo_dashboard_matches_local_ui_demo_shape():
    dashboard = build_demo_dashboard()
    workspace = dashboard.workspaces[0]

    assert dashboard.docker.status == "connected"
    assert workspace.id == "sag-commons-cli"
    assert workspace.docker.status == "running"
    assert workspace.latest_session == "CC-3"
    assert workspace.test.pass_count == 312
    assert workspace.test.pass_rate == 97.5
    assert workspace.evidence_status == "partial"
    assert "1.6.0" in f"{workspace.release} {workspace.tag}"


def test_demo_session_contains_evidence_context_card_and_report():
    detail = get_demo_session("CC-3")

    assert detail.id == "CC-3"
    assert detail.status == "completed"
    assert detail.evidence_status == "partial"
    assert detail.test.pass_rate == 97.5
    assert detail.evidence[0].source == "Project analyzer"
    assert detail.context is not None
    assert detail.result_card is not None
    assert detail.report_doc is not None


def test_demo_session_locks_local_ui_demo_facts():
    detail = get_demo_session("CC-3")
    evidence_text = " ".join(
        [
            detail.outcome,
            *(group.summary for group in detail.evidence),
            *(record.detail for group in detail.evidence for record in group.records),
        ]
    )

    assert detail.build.tool == "Maven 3.9.6"
    assert "JDK 11" in detail.build.note
    assert detail.build.artifact == "target/commons-cli-1.6.0.jar"
    assert detail.build.module_output_count == 3
    assert detail.test.evidence_layers is not None
    receipt = detail.test.evidence_layers.tests.claimed.receipt_executions
    assert receipt.availability == "available"
    assert (receipt.executed, receipt.passed, receipt.failed, receipt.errors, receipt.skipped) == (
        320,
        312,
        8,
        0,
        0,
    )
    assert receipt.executed == receipt.passed + receipt.failed + receipt.errors + receipt.skipped
    assert receipt.basis is not None
    assert receipt.basis.startswith("demo fixture:")
    assert "HelpFormatter" in evidence_text
    assert detail.report_doc is not None
    assert detail.report_doc.title == "setup-report-2026-06-06T0216.md"
    assert detail.rates == {
        "build": {
            "modules": {
                "numerator": 3,
                "denominator": 3,
                "rate": 100.0,
                "band": "fully",
            }
        }
    }
    # The demo card is derived by the same builder the CLI block and the
    # report table use, so it states the demo's own numbers, not a sentence
    # written beside them.
    card = detail.result_card
    assert card is not None
    assert card.verdict == "partial"
    assert card.row("build").headline == "3/3 modules built"
    assert card.row("tests").headline.startswith(
        "320 executed · 312 passed · 8 failed"
    )
    assert card.row("report").status == "delivered"
    # One attention item, counting the same eight failures the Tests row does.
    assert [item.title for item in card.attention] == ["commons-cli-core · 8 failing"]


def test_demo_session_detail_has_modules():
    detail = get_demo_session("CC-3")
    assert detail.modules, "demo detail should include modules"
    assert detail.module_summary is not None
    assert detail.module_summary.modules_total == 3
    assert detail.module_summary.modules_built == 3
    assert detail.module_summary.modules_failed == 0
    by_path = {m.path: m for m in detail.modules}
    assert by_path["validator"].build_status == "success"
    assert by_path["validator"].build_error_samples == []
    assert by_path["core"].failing_count == 8
    assert detail.module_summary.modules_with_test_failures == 1


def test_demo_session_says_it_is_a_demo_so_the_timeline_is_not_offered():
    """A demo detail stands for no run, and says so.

    `ReadModelBuilder.session_dir` refuses to name a directory under demo mode
    — there is no ledger anywhere to derive a trajectory from — so the detail
    carries the same fact and the frontend leaves the tab out instead of
    offering a panel that can only ever answer "unavailable".
    """
    assert get_demo_session("CC-3").demo is True


def test_demo_modules_carry_coverage():
    detail = get_demo_session("CC-3")
    by_path = {m.path: m for m in detail.modules}
    assert by_path["core"].line_rate is not None
    assert by_path["core"].branch_rate is not None
    assert detail.module_summary.line_rate is not None
