import json
from pathlib import Path

from sag.web.session_registry import SessionRegistry


def test_session_registry_reads_local_session_index(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "CC-3",
                        "workspace": "sag-commons-cli",
                        "title": "Build project",
                        "status": "running",
                        "entry": "CLI",
                        "start": "02:14:08",
                        "duration": "running · 2m 11s",
                        "build": "success",
                        "test": {
                            "state": "partial",
                            "pass": 312,
                            "fail": 8,
                            "skip": 0,
                            "total": 320,
                        },
                        "report": "ready",
                        "files": 7,
                        "evidence": 18,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert rows[0].id == "CC-3"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].test.pass_count == 312


def test_session_registry_returns_empty_for_missing_index(tmp_path: Path):
    assert SessionRegistry().read_index(tmp_path, "sag-commons-cli") == []


def test_session_registry_returns_empty_for_unreadable_or_invalid_indexes(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)

    for payload in ["{bad json", "[]", json.dumps({"sessions": {}})]:
        (setup_agent / "index.json").write_text(payload, encoding="utf-8")

        assert SessionRegistry().read_index(tmp_path, "sag-commons-cli") == []


def test_session_registry_skips_bad_rows_and_defaults_bad_numbers(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    "bad row",
                    {
                        "id": None,
                        "workspace": None,
                        "title": None,
                        "status": None,
                        "entry": None,
                        "start": None,
                        "finish": 17,
                        "duration": None,
                        "build": None,
                        "test": {"pass": "", "fail": None, "skip": "nope", "total": "12x"},
                        "report": None,
                        "files": "many",
                        "evidence": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert len(rows) == 1
    assert rows[0].id == "unknown"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].title == "Untitled task"
    assert rows[0].entry == "external"
    assert rows[0].start == "—"
    assert rows[0].finish == "17"
    assert rows[0].duration == "—"
    assert rows[0].test.pass_count == 0
    assert rows[0].test.fail_count == 0
    assert rows[0].test.skip_count == 0
    assert rows[0].test.total == 0
    assert rows[0].files == 0
    assert rows[0].evidence == 0
