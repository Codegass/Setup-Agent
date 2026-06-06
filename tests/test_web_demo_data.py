from sag.web.demo_data import build_demo_dashboard, get_demo_session


def test_demo_dashboard_matches_local_ui_demo_shape():
    dashboard = build_demo_dashboard()

    assert dashboard.docker.status == "connected"
    assert dashboard.workspaces[0].id == "sag-commons-cli"
    assert dashboard.workspaces[0].latest_session == "CC-3"
    assert dashboard.workspaces[0].test.pass_count == 312


def test_demo_session_contains_evidence_context_files_and_report():
    detail = get_demo_session("CC-3")

    assert detail.id == "CC-3"
    assert detail.evidence[0].source == "Project analyzer"
    assert detail.context is not None
    assert detail.files is not None
    assert detail.report_doc is not None
