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


def test_demo_session_contains_evidence_context_files_and_report():
    detail = get_demo_session("CC-3")

    assert detail.id == "CC-3"
    assert detail.status == "completed"
    assert detail.evidence_status == "partial"
    assert detail.test.pass_rate == 97.5
    assert detail.evidence[0].source == "Project analyzer"
    assert detail.context is not None
    assert detail.files is not None
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
    assert "HelpFormatter" in evidence_text
    assert detail.report_doc is not None
    assert detail.report_doc.title == "setup-report-2026-06-06T0216.md"


def test_demo_session_detail_has_modules():
    detail = get_demo_session("CC-3")
    assert detail.modules, "demo detail should include modules"
    assert detail.module_summary is not None
    assert detail.module_summary.modules_total >= 2
    by_path = {m.path: m for m in detail.modules}
    assert by_path["validator"].build_status == "failure"
    assert by_path["core"].failing_count == 2
    assert detail.module_summary.modules_with_test_failures == 1
