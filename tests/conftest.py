import pytest

from sag.agent.output_storage import OutputStorageManager
from sag.tools.base import bind_tool_result_output_storage


@pytest.fixture(scope="session")
def durable_tool_result_storage(tmp_path_factory):
    return OutputStorageManager(tmp_path_factory.mktemp("tool-result-output"))


@pytest.fixture(autouse=True)
def bind_durable_tool_result_storage(durable_tool_result_storage):
    with bind_tool_result_output_storage(
        durable_tool_result_storage,
        task_id="pytest",
        tool_name="test",
    ):
        yield
