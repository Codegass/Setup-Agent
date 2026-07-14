"""Host mirror for the web UI: copy result files out with get_archive (no exec,
works on stopped containers), serve reads from disk. Guards against the resource
drain where every 5s dashboard poll re-execed into every container."""

import io
import tarfile
import types

import pytest

from sag.web import session_mirror
from sag.web.session_mirror import MirrorReader, ensure_mirror


def _tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeContainer:
    def __init__(self, archives):
        self.archives = archives
        self.calls = []

    def get_archive(self, path):
        self.calls.append(path)
        if path not in self.archives:
            raise Exception("Not Found")  # mimics docker NotFound
        return iter([self.archives[path]]), {}


class FakeClient:
    def __init__(self, container):
        self.containers = types.SimpleNamespace(get=lambda name: container)


REPORT = "setup-report-20260708-101010.md"
ARCHIVES = {
    "/workspace/.setup_agent": _tar({
        ".setup_agent/sessions/index.json": b'{"sessions": []}',
        ".setup_agent/contexts/trunk_x.json": (
            '{"report_path": "/workspace/' + REPORT + '"}').encode(),
        ".setup_agent/report_metrics.json": b'{"pass_rate": 100}',
    }),
    "/workspace/.sag_last_comment.json": _tar({".sag_last_comment.json": b'{"comment": "done"}'}),
    "/workspace/" + REPORT: _tar({REPORT: b"# report"}),
}


@pytest.fixture(autouse=True)
def _clear_fetch_cache():
    session_mirror._last_fetch.clear()
    yield
    session_mirror._last_fetch.clear()


def test_mirror_extracts_results_and_report(tmp_path):
    container = FakeContainer(ARCHIVES)
    dest = ensure_mirror(FakeClient(container), "sag-x", running=False, logs_root=tmp_path)

    assert (dest / ".setup_agent" / "sessions" / "index.json").read_text() == '{"sessions": []}'
    assert (dest / ".setup_agent" / "report_metrics.json").is_file()
    assert (dest / ".sag_last_comment.json").is_file()
    # report .md name isn't derivable — resolved from the trunk's report_path
    assert (dest / REPORT).read_text() == "# report"


def test_stopped_container_mirrored_once(tmp_path):
    container = FakeContainer(ARCHIVES)
    client = FakeClient(container)
    ensure_mirror(client, "sag-x", running=False, logs_root=tmp_path)
    n = len(container.calls)
    ensure_mirror(client, "sag-x", running=False, logs_root=tmp_path)
    assert container.calls[n:] == []  # no re-fetch: stopped results are immutable


def test_running_container_refetches_after_ttl(tmp_path):
    container = FakeContainer(ARCHIVES)
    client = FakeClient(container)
    clock = [1000.0]
    ensure_mirror(client, "sag-x", running=True, logs_root=tmp_path, now=lambda: clock[0])
    n = len(container.calls)
    clock[0] += session_mirror.RUNNING_TTL_SECONDS + 1
    ensure_mirror(client, "sag-x", running=True, logs_root=tmp_path, now=lambda: clock[0])
    assert len(container.calls) > n  # refetched


def test_missing_path_skipped(tmp_path):
    container = FakeContainer({"/workspace/.setup_agent": ARCHIVES["/workspace/.setup_agent"]})
    dest = ensure_mirror(FakeClient(container), "sag-x", running=False, logs_root=tmp_path)
    assert dest is not None
    assert (dest / ".setup_agent" / "sessions" / "index.json").is_file()
    assert not (dest / ".sag_last_comment.json").exists()  # NotFound → skipped, no crash


def test_reader_answers_cat_and_finds(tmp_path):
    dest = ensure_mirror(FakeClient(FakeContainer(ARCHIVES)), "sag-x", running=False, logs_root=tmp_path)
    r = MirrorReader(dest)

    cat = r.execute_command("cat '/workspace/.setup_agent/sessions/index.json' 2>/dev/null")
    assert cat["exit_code"] == 0 and cat["output"] == '{"sessions": []}'

    report = r.execute_command(
        "find /workspace -maxdepth 1 -name 'setup-report-*.md' -type f 2>/dev/null | sort | tail -1")
    assert report["output"] == "/workspace/" + REPORT

    ctx = r.execute_command(
        "find /workspace/.setup_agent/contexts -maxdepth 2 -type f "
        "\\( -name 'trunk*.json' \\) -printf '%P\\n' 2>/dev/null || true")
    assert "trunk_x.json" in ctx["output"].splitlines()


def test_reader_missing_file_is_exit_1(tmp_path):
    r = MirrorReader(tmp_path / "nope")
    assert r.execute_command("cat '/workspace/.setup_agent/sessions/index.json' 2>/dev/null")["exit_code"] == 1
