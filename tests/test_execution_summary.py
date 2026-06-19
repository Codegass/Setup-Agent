"""Tests for ReActEngine.get_execution_summary runtime metadata.

Task 1 of the workbench-detail redesign: the execution summary must surface
the action model name and the iteration budget so the web read model can show
them on the session detail header.
"""

from unittest.mock import MagicMock

import pytest

from sag.agent.react_engine import ReActEngine


class _DummyContextManager:
    contexts_dir = "/workspace/.setup_agent/contexts"
    orchestrator = None

    def get_current_context_info(self):
        return {
            "context_type": "trunk",
            "context_id": "trunk",
            "goal": "Set up the repository",
            "progress": "0/1",
            "next_task": "task_1",
        }

    def load_trunk_context(self):
        return None


@pytest.fixture
def make_react_engine():
    def _factory():
        return ReActEngine(_DummyContextManager(), [])

    return _factory


def test_execution_summary_includes_model_and_budget(make_react_engine):
    engine = make_react_engine()  # fixture builds a ReActEngine with a stub Config
    # Config is a frozen Pydantic model; swap it for a stub so we can pin the
    # action model name returned to get_execution_summary.
    engine.config = MagicMock()
    engine.config.get_litellm_model_name = MagicMock(return_value="claude-sonnet-4.5")
    engine.max_iterations = 40
    summary = engine.get_execution_summary()
    assert summary["model"] == "claude-sonnet-4.5"
    assert summary["max_iterations"] == 40
    engine.config.get_litellm_model_name.assert_called_once_with("action")
