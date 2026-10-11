"""file_io reads of a receipt path return its compact view, not raw JSON."""

import hashlib
import json
from io import StringIO

import pytest
from test_invocation_receipts import minimal_valid_receipt

from sag.agent.receipt_view import build_receipt_view
from sag.tools.base import ToolError
from sag.tools.file_io import FileIOTool
from sag.tools.output_paging import read_text_page

RAW_DIR = "/workspace/.setup_agent/invocation_receipts"
VIEW_DIR = "/workspace/.setup_agent/receipt_views"


def raw_bytes(receipt):
    # The production writer stores exactly this canonical body.
    return json.dumps(receipt, sort_keys=True)


def view_body(receipt):
    raw = raw_bytes(receipt)
    view = build_receipt_view(receipt, source_sha256=hashlib.sha256(raw.encode()).hexdigest())
    return json.dumps(view, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


class FakeContainer:
    """Serve `cat` of known files; record every command the tool sends."""

    def __init__(self, files):
        self.files = dict(files)
        self.commands = []

    def execute_command(self, command, truncate_output=True, **kwargs):
        self.commands.append(command)
        if "read_text_page" in command:
            # A paged read: one real page of a tiny file, for whatever path it named.
            page = read_text_page(StringIO("paged text\n"), 0, None, 0, 20_000)
            return {"success": True, "exit_code": 0, "output": json.dumps(page)}
        for path, body in self.files.items():
            if command.startswith("cat ") and path in command:
                return {"success": True, "exit_code": 0, "output": body}
        return {"success": False, "exit_code": 1, "output": "cat: No such file or directory"}


def paged_reads(container):
    return [command for command in container.commands if "read_text_page" in command]


def test_reading_a_raw_receipt_path_returns_the_receipt_view():
    receipt = minimal_valid_receipt("inv-fileio-1")
    view_path = f"{VIEW_DIR}/inv-fileio-1.json"
    container = FakeContainer({view_path: view_body(receipt)})
    raw_path = f"{RAW_DIR}/inv-fileio-1.json"

    result = FileIOTool(container).safe_execute(action="read", path=raw_path)

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {view_path}")
    assert "Invocation:" in result.output and "Command: mvn test" in result.output
    assert '"report_delta"' not in result.output and '{"' not in result.output
    assert "machine-checked receipt" in result.output
    assert "Next read" not in result.output
    assert result.raw_output == result.output
    assert result.metadata["receipt_redirect"] is True
    assert result.metadata["view_source"] == "receipt_views"
    assert result.metadata["view_path"] == view_path
    assert result.metadata["path"] == raw_path
    assert paged_reads(container) == []
    assert all(raw_path not in command for command in container.commands)


def test_reading_a_receipt_view_path_returns_the_rendered_view():
    receipt = minimal_valid_receipt("inv-fileio-2")
    view_path = f"{VIEW_DIR}/inv-fileio-2.json"
    container = FakeContainer({view_path: view_body(receipt)})

    result = FileIOTool(container).safe_execute(
        action="read", path=view_path, start_line=40, end_line=90, max_chars=500
    )

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {view_path}")
    assert "Invocation:" in result.output
    assert result.metadata["view_source"] == "receipt_views"
    assert paged_reads(container) == []


def test_missing_view_falls_back_to_building_it_from_the_raw_receipt():
    receipt = minimal_valid_receipt("inv-fileio-3")
    raw_path = f"{RAW_DIR}/inv-fileio-3.json"
    body = raw_bytes(receipt)
    container = FakeContainer({raw_path: body})

    # A non-normalised spelling of the same raw receipt path still redirects.
    result = FileIOTool(container).safe_execute(
        action="read", path="/workspace/.setup_agent/./invocation_receipts/inv-fileio-3.json"
    )

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {raw_path}")
    assert "Command: mvn test" in result.output
    assert result.metadata["view_source"] == "built_from_raw_receipt"
    assert result.metadata["view_path"] == f"{VIEW_DIR}/inv-fileio-3.json"
    assert result.metadata["raw_receipt_bytes"] == len(body.encode())
    assert paged_reads(container) == []


def test_unreadable_receipt_and_view_is_a_read_failure():
    container = FakeContainer({f"{RAW_DIR}/broken.json": "{not json"})
    tool = FileIOTool(container)
    with pytest.raises(ToolError) as missing:
        tool._read(f"{RAW_DIR}/absent.json", 0, None)
    assert missing.value.error_code == "READ_FAILED"
    with pytest.raises(ToolError) as broken:
        tool._read(f"{RAW_DIR}/broken.json", 0, None)
    assert broken.value.error_code == "READ_FAILED"
    assert paged_reads(container) == []


def test_ordinary_file_reads_are_unchanged():
    container = FakeContainer({})
    result = FileIOTool(container).safe_execute(action="read", path="/workspace/proj/pom.xml")
    assert result.succeeded, result.error
    assert result.raw_output == "paged text\n"
    assert result.metadata["output_page"] is True
    assert "receipt_redirect" not in result.metadata
    assert len(paged_reads(container)) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/project/.setup_agent/invocation_receipts/x.json",
        f"{RAW_DIR}/sub/x.json",
        f"{VIEW_DIR}/nested/x.json",
        f"{RAW_DIR}/x.json.bak",
        f"{RAW_DIR}/notes.txt",
        "/workspace/.setup_agent/other/x.json",
    ],
)
def test_lookalike_paths_are_not_redirected(path):
    container = FakeContainer({})
    result = FileIOTool(container).safe_execute(action="read", path=path)
    assert result.succeeded, result.error
    assert "receipt_redirect" not in result.metadata
    assert len(paged_reads(container)) == 1
    assert container.commands == paged_reads(container)


def test_description_names_the_receipt_redirect():
    assert "returns its compact reading view, not the raw JSON" in FileIOTool(None).description
