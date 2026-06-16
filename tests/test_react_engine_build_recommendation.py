"""ReActEngine surfaces the analyzer's build recommendation in the build/test
intro, read from the trunk environment_summary (best-effort)."""

from types import SimpleNamespace

from sag.agent.react_engine import ReActEngine


def _engine_with_recommendation(rec):
    engine = ReActEngine.__new__(ReActEngine)

    class FakeCM:
        def load_trunk_context(self):
            return SimpleNamespace(
                environment_summary=({"build_recommendation": rec} if rec else {})
            )

    engine.context_manager = FakeCM()
    return engine


def test_recommended_build_line_renders_target_and_goal():
    engine = _engine_with_recommendation(
        {
            "build_system": "maven",
            "goal": "install",
            "build_root": "/workspace/bigtop",
            "is_aggregator_only": False,
            "rationale": "Aggregator over Groovy modules.",
        }
    )
    line = engine._recommended_build_line()
    assert "maven 'install'" in line
    assert "/workspace/bigtop" in line


def test_recommended_build_line_directs_block_for_meta_project():
    engine = _engine_with_recommendation(
        {"is_aggregator_only": True, "rationale": "No Java compile target."}
    )
    line = engine._recommended_build_line()
    assert "NONE" in line
    assert "blocked" in line.lower()


def test_recommended_build_line_absent_returns_none():
    assert _engine_with_recommendation(None)._recommended_build_line() is None


def test_test_phase_line_points_at_separate_test_target():
    engine = _engine_with_recommendation(
        {
            "build_root": "/workspace/bigtop/bigtop-test-framework",
            "build_system": "maven",
            "test_root": "/workspace/bigtop/bigtop-data-generators",
            "test_system": "gradle",
        }
    )
    line = engine._recommended_build_line("test")
    assert "gradle" in line
    assert "/workspace/bigtop/bigtop-data-generators" in line


def test_test_phase_line_suppressed_when_tests_co_located():
    engine = _engine_with_recommendation(
        {
            "build_root": "/workspace/p",
            "build_system": "maven",
            "test_root": "/workspace/p",
            "test_system": "maven",
        }
    )
    assert engine._recommended_build_line("test") is None


def test_recommended_build_line_swallows_errors():
    engine = ReActEngine.__new__(ReActEngine)

    class BoomCM:
        def load_trunk_context(self):
            raise RuntimeError("no container")

    engine.context_manager = BoomCM()
    assert engine._recommended_build_line() is None
