"""The model can recover omitted text through the public tools, without Docker."""

import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from test_output_storage import FakeOutputStorageOrchestrator
from test_tool_orchestration_parameters import _orchestrator

from sag.agent.output_storage import OutputStorageManager, attach_durable_output_ref
from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolCall, ToolExecution, format_tool_result
from sag.tools.base import (
    ActualToolExecution,
    BaseTool,
    OutputPersistenceError,
    ToolResult,
    bind_tool_result_output_storage,
)
from sag.tools.bash import BashTool, BashToolConfig
from sag.tools.file_io import FileIOTool
from sag.tools.internal.output_search_tool import OutputSearchTool
from sag.tools.output_paging import read_text_page
from sag.tools.search_tool import SearchTool


class ShellTransport:
    """Run actual read/search commands; model the older transport's display cap."""

    def __init__(self, root):
        self.root = str(root)
        self.project_name = "output-access"

    def execute_command(self, command, workdir=None, truncate_output=True, **kwargs):
        proc = subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=workdir or self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = proc.stdout + proc.stderr
        if truncate_output and len(output) > 10_000:
            output = output[:4_000] + "[transport preview]" + output[-3_000:]
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": output,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


class ResultTool(BaseTool):
    def __init__(self, result):
        super().__init__("result", "return observed text")
        self.result = result

    def execute(self):
        return self.result


def test_search_stores_full_matches_before_the_model_preview(tmp_path):
    # The RocketMQ failure shape: a matching XML line whose middle disappears
    # from a head/tail preview. The returned ref must retain that middle.
    text = "<testsuite " + "x" * 23_000 + "MIDDLE_SENTINEL" + "y" * 24_000 + "/>\n"
    (tmp_path / "report.xml").write_text(text)
    storage = OutputStorageManager(tmp_path / "outputs")
    tool = SearchTool(ShellTransport(tmp_path))
    with bind_tool_result_output_storage(storage, tool_name="search"):
        result = tool.safe_execute(target=f"file:{tmp_path / 'report.xml'}", pattern="testsuite")
    assert result.succeeded
    assert "MIDDLE_SENTINEL" not in result.output
    complete = storage.retrieve_output(result.output_ref)
    assert "MIDDLE_SENTINEL" in complete
    assert len(complete) > 47_000
    assert Path(result.metadata["output_path"]).read_text() == complete
    message = format_tool_result("search", result)
    assert result.output_ref in message and result.metadata["output_path"] in message
    assert "cannot be retrieved" not in message

    # Use the same public facade that the model has, not a storage-only probe.
    reader = SearchTool(None, output_search=OutputSearchTool(contexts_dir=storage.storage_dir))
    found = reader.safe_execute(target=result.output_ref, pattern="MIDDLE_SENTINEL", max_results=1)
    assert found.metadata["total_matches"] == 1
    page = reader.safe_execute(target=result.output_ref, column_offset=22_995, max_chars=60)
    assert "MIDDLE_SENTINEL" in page.output


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a\n",
        "no final newline",
        "中文🙂\r\n" * 100,
        "x" * 48_000 + "MIDDLE_SENTINEL" + "y" * 5_000 + "\nend\n",
    ],
)
def test_pages_reassemble_exact_text_including_long_lines(text):
    args = {"max_chars": 173}
    pages = []
    for _ in range(1_000):
        page = read_text_page(StringIO(text), **args)
        pages.append(page["text"])
        if page["next"] is None:
            break
        assert page["text"], "a nonterminal page must make progress"
        args.update(page["next"])
    else:
        pytest.fail("pagination did not terminate")
    assert "".join(pages) == text


def test_file_reader_executes_bounded_pages_in_the_file_environment(tmp_path):
    # Quoted path, CRLF, a long single line and a final unterminated line.
    path = tmp_path / "odd ' name $(not-a-command).log"
    text = "first\r\n" + "汉🙂" * 15_000 + "\r\nlast"
    path.write_bytes(text.encode())
    tool = FileIOTool(ShellTransport(tmp_path))
    args = {"action": "read", "path": str(path), "max_chars": 13_000}
    collected = []
    for _ in range(10):
        result = tool.safe_execute(**args)
        assert result.succeeded, result.error
        collected.append(result.raw_output)
        assert not result.metadata.get("output_truncated")
        if result.metadata["next"] is None:
            break
        # Follow the exact continuation presented to the model.
        encoded = result.output.split("Next read: ", 1)[1].rsplit("]", 1)[0]
        args = json.loads(encoded)
    assert "".join(collected) == text


def test_file_range_does_not_include_unrequested_lines(tmp_path):
    path = tmp_path / "lines.log"
    path.write_text("zero\none\ntwo\nthree\n")
    result = FileIOTool(ShellTransport(tmp_path)).safe_execute(
        action="read",
        path=str(path),
        start_line=1,
        end_line=3,
        max_chars=100,
    )
    assert result.succeeded
    assert result.raw_output == "one\ntwo\n"
    assert result.metadata["complete"] and result.metadata["next"] is None


def test_ref_reader_keeps_a_large_requested_page_and_empty_is_not_missing(tmp_path):
    storage = OutputStorageManager(tmp_path)
    text = "prefix\n" + "z" * 16_000 + "CRITICAL_END\n"
    ref = storage.store_output("task", "bash", text)
    reader = SearchTool(None, output_search=OutputSearchTool(contexts_dir=tmp_path))
    result = reader.safe_execute(target=ref, max_chars=20_000)
    assert "CRITICAL_END" in result.output and "z" * 16_000 in result.output
    assert result.raw_output == text and result.output_ref == ref
    assert not result.metadata.get("output_truncated")
    empty = storage.store_output("task", "bash", "")
    assert reader.safe_execute(target=empty).succeeded
    assert not reader.safe_execute(target="output_missing").succeeded


def test_ref_search_honors_case_and_exposes_all_match_pages(tmp_path):
    storage = OutputStorageManager(tmp_path)
    ref = storage.store_output("task", "bash", "Error one\nerror two\nError three\nError four\n")
    reader = SearchTool(None, output_search=OutputSearchTool(contexts_dir=tmp_path))
    first = reader.safe_execute(target=ref, pattern="Error", max_results=2, context_lines=0)
    assert first.metadata["total_matches"] == 3
    assert "error two" not in first.output and "Error four" not in first.output
    assert "offset=2" in first.output
    second = reader.safe_execute(
        target=ref, pattern="Error", offset=2, max_results=2, context_lines=0
    )
    assert "Error four" in second.output and second.metadata["next_offset"] is None
    insensitive = reader.safe_execute(target=ref, pattern="Error", ignore_case=True)
    assert insensitive.metadata["total_matches"] == 4


def test_bash_sed_does_not_filter_or_reorder_selected_lines(tmp_path):
    text = "".join(f"row:{n:03d} ordinary content\n" for n in range(120))
    path = tmp_path / "selected.log"
    path.write_text(text)
    tool = BashTool(ShellTransport(tmp_path), BashToolConfig(add_sag_cli_marker=False))
    result = tool.safe_execute(
        command="sed -n '1,120p' selected.log", working_directory=str(tmp_path)
    )
    assert result.succeeded, result.error
    assert result.output == text


def test_bash_retains_the_middle_before_transport_and_model_caps(tmp_path):
    text = "line\n" * 5_000 + "MIDDLE_SENTINEL\n" + "tail\n" * 5_000
    path = tmp_path / "large.log"
    path.write_text(text)
    storage = OutputStorageManager(tmp_path / "outputs")
    tool = BashTool(ShellTransport(tmp_path), BashToolConfig(add_sag_cli_marker=False))
    with bind_tool_result_output_storage(storage):
        result = tool.safe_execute(
            command="sed -n '1,10001p' large.log", working_directory=str(tmp_path)
        )
    assert result.succeeded, result.error
    assert result.raw_output == text
    assert storage.retrieve_output(result.output_ref) == text


def test_container_output_file_preserves_unicode_newlines_and_execution_trace(tmp_path):
    orch = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(tmp_path, orch)
    text = "  中文 `$(not executed)`\r\n" * 8_000 + "\n"
    actual = ActualToolExecution(
        "bash", {"command": "sed"}, ToolResult.completed_success(output="ok")
    )
    result = ToolResult.completed_success(output=text).with_execution_trace([actual])
    with bind_tool_result_output_storage(storage):
        saved = ResultTool(result).safe_execute()
    path = saved.metadata["output_path"]
    assert path.startswith("/workspace/.setup_agent/contexts/")
    assert orch.files[path] == text
    assert storage.retrieve_output(saved.output_ref) == text
    assert saved.execution_trace == (actual,)
    assert "Full output file (container)" in format_tool_result("bash", saved)


def test_failed_plain_text_copy_does_not_invent_a_path_or_lose_the_ref(tmp_path):
    orch = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(tmp_path, orch)
    text = "data\n" * 3_000
    ref = storage.store_output("test", "bash", text)
    orch.failed_write_paths.add(f"/workspace/.setup_agent/contexts/{ref}.log.tmp")
    result = attach_durable_output_ref(
        ToolResult.completed_success(output="preview", raw_output=text, output_ref=ref),
        storage,
        task_id="test",
        tool_name="bash",
    )
    assert "output_path" not in result.metadata
    assert storage.retrieve_output(result.output_ref) == text
    message = format_tool_result("bash", result)
    assert "file unavailable" in message and "Full output file (container)" not in message


def test_an_existing_output_path_is_verified_before_it_is_returned_again(tmp_path):
    orch = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(tmp_path, orch)
    text = "original\n" * 2_000
    result = attach_durable_output_ref(
        ToolResult.completed_success(output=text), storage, task_id="test", tool_name="bash"
    )
    path = result.metadata["output_path"]
    orch.files[path] = "replaced contents"
    refreshed = attach_durable_output_ref(result, storage, task_id="test", tool_name="bash")
    assert refreshed.metadata["output_path"] == path
    assert orch.files[path] == text


def test_unbound_tool_still_preserves_raw_output_for_engine_persistence(tmp_path):
    text = "original\n" * 4_000
    with bind_tool_result_output_storage(None):
        result = ResultTool(ToolResult.completed_success(output=text)).safe_execute()
    assert result.raw_output == text
    storage = OutputStorageManager(tmp_path)
    result = attach_durable_output_ref(result, storage, task_id="test", tool_name="result")
    assert storage.retrieve_output(result.output_ref) == text


def test_bash_rg_reads_real_matches_with_context(tmp_path):
    path = tmp_path / "source.log"
    path.write_text("before\nMIDDLE_SENTINEL\nafter\n")
    tool = BashTool(ShellTransport(tmp_path), BashToolConfig(add_sag_cli_marker=False))
    result = tool.safe_execute(
        command="rg -n -C 1 MIDDLE_SENTINEL source.log", working_directory=str(tmp_path)
    )
    assert result.succeeded, result.error
    assert "1-before" in result.output and "2:MIDDLE_SENTINEL" in result.output
    assert "3-after" in result.output


def test_output_index_is_not_subject_to_the_transport_display_cap(tmp_path):
    class CappedIndex(FakeOutputStorageOrchestrator):
        def execute_command(self, command, truncate_output=True):
            result = super().execute_command(command)
            if "cat " in command and command.endswith("output_index.json") and truncate_output:
                # A valid but incomplete index is harder to detect than invalid JSON.
                result["output"] = "{}"
            return result

    orch = CappedIndex()
    writer = OutputStorageManager(tmp_path, orch)
    refs = [writer.store_output("test", "bash", f"output {i}") for i in range(25)]
    reader = OutputStorageManager(tmp_path, orch)
    assert set(refs).issubset(reader.current_index)
    assert reader.retrieve_output(refs[12]) == "output 12"


def test_pagination_reaches_the_model_through_the_real_tool_orchestrator(tmp_path):
    storage = OutputStorageManager(tmp_path)
    ref = storage.store_output("test", "bash", "x" * 21_000 + "CRITICAL_END\n")
    reader = SearchTool(None, output_search=OutputSearchTool(contexts_dir=tmp_path))
    controller, _, _, _ = _orchestrator(tools={"search": reader}, output_storage=storage)
    first = controller.execute(
        ToolCall(name="search", raw_params={"target": ref, "max_chars": 20_000})
    )
    assert first.result.succeeded
    assert "x" * 20_000 in first.observation_text
    assert "Next read:" in first.observation_text
    second = controller.execute(
        ToolCall(
            name="search",
            raw_params={
                "target": ref,
                **first.result.metadata["next"],
                "max_chars": 20_000,
            },
        )
    )
    assert second.result.succeeded
    assert "CRITICAL_END" in second.observation_text
    assert "Source output ref:" in second.observation_text


def test_a_failed_long_command_keeps_its_status_and_complete_diagnostics(tmp_path):
    storage = OutputStorageManager(tmp_path)
    text = "noise\n" * 4_000 + "ROOT_CAUSE\n" + "tail\n" * 4_000
    with bind_tool_result_output_storage(storage):
        original = ToolResult.completed_failure(output=text, error="command exited 1")
        result = ResultTool(original).safe_execute()
    assert not result.succeeded and result.operation_outcome.value == "failed"
    assert len(result.output) < len(text)
    assert storage.retrieve_output(result.output_ref) == text
    assert "ROOT_CAUSE" in Path(result.metadata["output_path"]).read_text()


def test_persistence_failure_preserves_the_execution_which_already_happened():
    class BrokenStorage:
        def store_output(self, **kwargs):
            return ""

        store_emergency_output = store_output

    with bind_tool_result_output_storage(BrokenStorage()):
        with pytest.raises(OutputPersistenceError) as raised:
            ResultTool(ToolResult.completed_success(output="observed\n" * 4_000)).safe_execute()
    assert raised.value.execution_id
    assert raised.value.draft.invocation_status.value == "completed"
    assert raised.value.draft.operation_outcome.value == "success"
    assert raised.value.draft.output_ref is None


def test_a_file_attached_at_recording_time_reaches_the_final_observation(tmp_path):
    storage = OutputStorageManager(tmp_path)
    text = "native output\n" * 2_000
    ref = storage.store_output("test", "build", text)
    original = ToolResult.completed_success(
        output="short build summary", raw_output=text, output_ref=ref
    )
    call = ToolCall(name="build", raw_params={})
    execution = ToolExecution(
        call=call,
        result=original,
        status="success",
        raw_params={},
        observation_text=format_tool_result("build", original),
        attempted_execution=True,
    )
    engine = ReActEngine.__new__(ReActEngine)
    engine._record_tool_execution = (
        lambda tool, params, result, **kwargs: attach_durable_output_ref(
            result, storage, task_id="test", tool_name=tool
        )
    )
    engine._record_execution_bundle(execution, call)
    assert execution.result is not original
    assert execution.result.metadata["output_path"] in execution.observation_text
    assert "output_path" not in original.metadata
