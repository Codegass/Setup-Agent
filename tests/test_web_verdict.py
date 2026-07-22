from sag.web.verdict import compose_verdict

MS = {"modulesTotal": 4, "modulesBuilt": 3, "modulesFailed": 1, "singleModule": False}
TEST = {"state": "partial", "pass": 1186, "fail": 7, "total": 1205}


def test_partial_verdict():
    v = compose_verdict(
        build={"state": "success"}, test=TEST, module_summary=MS, outcome="⚠️ PARTIAL", blocker=None
    )
    assert v["tone"] == "attention"
    assert "3 of 4 modules" in v["headline"]
    assert "7 of 1,205 tests failing" in v["headline"]
    assert "review before promoting" in v["headline"]


def test_success_verdict():
    v = compose_verdict(
        build={"state": "success"},
        test={"state": "success", "pass": 1205, "fail": 0, "total": 1205},
        module_summary={"modulesTotal": 4, "modulesBuilt": 4, "singleModule": False},
        outcome="✅ SUCCESS",
        blocker=None,
    )
    assert v["tone"] == "success"
    assert "all 4 modules" in v["headline"]


def test_failed_verdict_with_blocker_hint():
    v = compose_verdict(
        build={"state": "failed"},
        test={"state": "none", "pass": 0, "fail": 0, "total": 0},
        module_summary={
            "modulesTotal": 4,
            "modulesBuilt": 0,
            "modulesFailed": 1,
            "singleModule": False,
        },
        outcome="❌ FAILED",
        blocker={"hint": "fix the missing dependency in acme-cli"},
    )
    assert v["tone"] == "failed"
    assert v["detail"] == "fix the missing dependency in acme-cli"


def test_single_module_phrasing():
    v = compose_verdict(
        build={"state": "success"},
        test={"state": "success", "pass": 320, "fail": 0, "total": 320},
        module_summary={"singleModule": True},
        outcome="✅ SUCCESS",
        blocker=None,
    )
    assert "module" not in v["headline"].lower() or "modules" not in v["headline"]
    assert "320 tests passing" in v["headline"]


def test_returns_none_when_empty():
    assert (
        compose_verdict(build=None, test=None, module_summary=None, outcome="", blocker=None)
        is None
    )


def test_test_errors_are_rendered_as_non_passing():
    verdict = compose_verdict(
        build={"state": "partial"},
        test={"state": "partial", "pass": 0, "fail": 0, "errors": 328, "total": 328},
        module_summary=None,
        outcome="PARTIAL",
        blocker=None,
        canonical_verdict="partial",
        verdict_source="snapshot",
    )

    assert "328 of 328 tests failing" in verdict["headline"]
    assert "tests passing" not in verdict["headline"]
