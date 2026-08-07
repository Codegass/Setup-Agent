# tests/test_result_hint_vocabulary.py
"""Tool RESULTS must teach the registered surface, like the prompts do (#30).

Stage 1 swept react_engine.yaml (test_prompt_vocabulary.py) but the runtime
hint strings rendered INTO tool results kept teaching output_search — a tool
the model can no longer route to directly. The harness proposing a call that
cannot route is the #19 defect class; live p8a/p8b kafka sessions carried the
stale hint next to the envelope's correct one. Two contracts here:

1. No model-facing hint proposes an ``output_search(...)`` call.
2. The replacement is REACHABLE: ``search(target='output_...')`` with no
   pattern retrieves the stored output — grep-with-a-guessed-pattern must not
   be the only reachable action, because guessing what to look for in a log
   it has never seen is exactly what a weak model cannot do.
"""

from pathlib import Path

from sag.tools.base import BaseTool, ToolResult
from sag.tools.search_tool import SearchTool

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "sag"

# The literal call shape the stale hints taught. Prose mentions of the
# internal delegate ("output_search surfaces the real failure") are fine;
# proposing the CALL is not.
STALE_CALL = "output_search(action="


def test_no_tool_module_renders_an_output_search_call():
    offenders = [
        str(path.relative_to(REPO_SRC))
        for path in (REPO_SRC / "tools").rglob("*.py")
        if STALE_CALL in path.read_text()
    ]
    assert offenders == [], f"stale output_search call hints still rendered by: {offenders}"


def test_truncation_guidance_teaches_the_search_tool():
    guidance = BaseTool._truncation_guidance("output_abc123")
    assert "output_abc123" in guidance
    assert "search(target='output_abc123'" in guidance
    assert STALE_CALL not in guidance


class _RecordingOutputSearch:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult.completed_success(output="stored output body")


def test_ref_target_without_pattern_retrieves_the_stored_output():
    recorder = _RecordingOutputSearch()
    tool = SearchTool(object(), output_search=recorder, web_search=None)

    result = tool.execute(target="output_5b9a")

    assert result.succeeded and "stored output body" in result.output
    assert recorder.calls == [{"action": "retrieve", "ref_id": "output_5b9a"}]


def test_ref_target_with_pattern_still_greps():
    recorder = _RecordingOutputSearch()
    tool = SearchTool(object(), output_search=recorder, web_search=None)

    tool.execute(target="output_5b9a", pattern="Tests run")

    assert recorder.calls[0]["action"] == "grep"
    assert recorder.calls[0]["grep_pattern"] == "Tests run"


def test_search_tool_description_teaches_the_no_pattern_retrieve():
    tool = SearchTool(object(), output_search=_RecordingOutputSearch(), web_search=None)
    assert "omit pattern" in tool.description


def test_legacy_alias_preserves_retrieve_intent():
    """A model calling output_search(action='retrieve') out of old habit must
    land on the retrieve path, not on a silent grep-all-lines downgrade."""
    from sag.agent.tool_parameters import ToolParameterNormalizer

    normalizer = ToolParameterNormalizer(
        tools={}, successful_states={}, repository_url=""
    )
    name, params = normalizer.resolve_legacy_alias(
        "output_search", {"action": "retrieve", "ref_id": "output_5b9a"}
    )
    assert name == "search"
    assert params["target"] == "output_5b9a"
    assert not params.get("pattern"), (
        "retrieve intent must map to the no-pattern retrieve, not pattern='.'"
    )

    name, params = normalizer.resolve_legacy_alias(
        "output_search",
        {"action": "grep", "ref_id": "output_5b9a", "grep_pattern": "FAIL"},
    )
    assert params["pattern"] == "FAIL"
