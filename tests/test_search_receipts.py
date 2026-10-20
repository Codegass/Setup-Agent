"""search(target='file:<receipt path>') returns the receipt view, not raw JSON.

file_io already answers a read of a receipt path with its reading view; the same
receipt reached through search's `file:` form used to be grepped, and an empty
pattern (grep for `.`) put up to max_chars of raw receipt JSON into the window.
"""

import json

import pytest
from test_file_io_receipts import RAW_DIR, VIEW_DIR, raw_bytes, view_body
from test_invocation_receipts import minimal_valid_receipt

from sag.tools.search_tool import SearchTool


class FakeContainer:
    """Serve `cat` of known files and one grep hit; record every command sent."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "grep -" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": "3:matched line",
                "stdout": "3:matched line",
                "stderr": "",
            }
        for path, body in self.files.items():
            if command.startswith("cat ") and path in command:
                return {"success": True, "exit_code": 0, "output": body}
        return {"success": False, "exit_code": 1, "output": "cat: No such file or directory"}


def greps(container):
    return [command for command in container.commands if "grep -" in command]


NOT_APPLIED = "was not applied"


def test_receipt_path_without_a_pattern_returns_the_view():
    receipt = minimal_valid_receipt("inv-search-1")
    view_path = f"{VIEW_DIR}/inv-search-1.json"
    raw_path = f"{RAW_DIR}/inv-search-1.json"
    container = FakeContainer({view_path: view_body(receipt)})

    result = SearchTool(container).safe_execute(target=f"file:{raw_path}")

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {view_path}")
    assert "Invocation:" in result.output and "Command: mvn test" in result.output
    assert '{"' not in result.output
    assert NOT_APPLIED not in result.output
    assert result.metadata["receipt_redirect"] is True
    assert result.metadata["view_source"] == "receipt_views"
    assert result.metadata["view_path"] == view_path
    assert result.metadata["path"] == raw_path
    assert greps(container) == []
    assert all(raw_path not in command for command in container.commands)


def test_receipt_path_with_a_pattern_returns_the_whole_view_and_says_so():
    receipt = minimal_valid_receipt("inv-search-2")
    view_path = f"{VIEW_DIR}/inv-search-2.json"
    container = FakeContainer({view_path: view_body(receipt)})

    result = SearchTool(container).safe_execute(
        target=f"file:{RAW_DIR}/inv-search-2.json", pattern="testcase_execution_totals"
    )

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {view_path}")
    assert "Report execution totals:" in result.output
    last = result.output.splitlines()[-1]
    assert NOT_APPLIED in last and "'testcase_execution_totals'" in last
    assert "receipt" in last and "whole" in last
    assert result.raw_output == result.output
    assert result.metadata["receipt_redirect"] is True
    assert result.metadata["pattern_applied"] is False
    assert greps(container) == []


def test_view_path_returns_the_rendered_view():
    receipt = minimal_valid_receipt("inv-search-3")
    view_path = f"{VIEW_DIR}/inv-search-3.json"
    container = FakeContainer({view_path: view_body(receipt)})

    result = SearchTool(container).safe_execute(target=f"file:{view_path}", pattern="exit_code")

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {view_path}")
    assert result.metadata["view_source"] == "receipt_views"
    assert NOT_APPLIED in result.output.splitlines()[-1]
    assert greps(container) == []


def test_missing_view_is_built_from_the_raw_receipt():
    receipt = minimal_valid_receipt("inv-search-4")
    raw_path = f"{RAW_DIR}/inv-search-4.json"
    body = raw_bytes(receipt)
    container = FakeContainer({raw_path: body})

    result = SearchTool(container).safe_execute(
        target="file:/workspace/.setup_agent/./invocation_receipts/inv-search-4.json"
    )

    assert result.succeeded, result.error
    assert result.output.startswith(f"Receipt view: {raw_path}")
    assert result.metadata["view_source"] == "built_from_raw_receipt"
    assert result.metadata["raw_receipt_bytes"] == len(body.encode())
    assert greps(container) == []


@pytest.mark.parametrize("pattern", ["", "exit_code"])
def test_missing_receipt_is_a_read_failure_not_a_grep(pattern):
    container = FakeContainer({f"{RAW_DIR}/broken.json": "{not json"})
    tool = SearchTool(container)

    missing = tool.safe_execute(target=f"file:{RAW_DIR}/absent.json", pattern=pattern)
    broken = tool.safe_execute(target=f"file:{RAW_DIR}/broken.json", pattern=pattern)

    for result in (missing, broken):
        assert not result.succeeded
        assert result.error_code == "READ_FAILED"
    assert greps(container) == []


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/project/package.json",
        "/workspace/.setup_agent/other/x.json",
        "/workspace/project/.setup_agent/invocation_receipts/x.json",
        f"{RAW_DIR}/sub/x.json",
        f"{VIEW_DIR}/nested/x.json",
        f"{RAW_DIR}/x.json.bak",
    ],
)
def test_other_json_files_are_still_grepped(path):
    container = FakeContainer({})

    result = SearchTool(container).safe_execute(target=f"file:{path}", pattern="version")

    assert result.succeeded, result.error
    assert "matched line" in result.output
    assert "receipt_redirect" not in result.metadata
    assert len(greps(container)) == 1
    assert container.commands == greps(container)


def test_description_says_a_receipt_path_returns_its_view():
    description = SearchTool(None).description
    assert (
        "'file:' on a receipt under /workspace/.setup_agent/invocation_receipts or "
        "receipt_views returns its compact reading view whole, not the raw JSON; "
        "pattern is not applied there."
    ) in description
    for word in ("sealed", "canonical", "snapshot"):
        assert word not in description.lower()


def test_search_and_file_io_return_the_same_view():
    from sag.tools.file_io import FileIOTool

    receipt = minimal_valid_receipt("inv-search-5")
    raw_path = f"{RAW_DIR}/inv-search-5.json"
    files = {f"{VIEW_DIR}/inv-search-5.json": view_body(receipt)}

    searched = SearchTool(FakeContainer(files)).safe_execute(target=f"file:{raw_path}")
    read = FileIOTool(FakeContainer(files)).safe_execute(action="read", path=raw_path)

    assert searched.output == read.output
    for key in ("path", "receipt_redirect", "view_path", "view_source"):
        assert searched.metadata[key] == read.metadata[key]
    json.dumps(searched.metadata)  # stays serialisable for the output ledger
