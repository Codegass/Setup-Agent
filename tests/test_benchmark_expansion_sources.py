import io
import tarfile

from scripts.benchmark_expansion_sources import freeze_version
from scripts.benchmark_source_audit import blob_sha, ref, write_json


def fixture(tmp_path, monkeypatch, *, omit_java=False, omit_other=False):
    sha = "a" * 40
    directory = tmp_path / "versions/example/library" / sha
    directory.mkdir(parents=True)
    contents = {"src/main/java/Example.java": b"class Example {}\n", "pom.xml": b"<project/>"}
    path = directory / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, body in contents.items():
            if (omit_java and name.endswith(".java")) or (omit_other and name == "pom.xml"):
                continue
            info = tarfile.TarInfo("library-" + sha + "/" + name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    url = f"https://codeload.github.com/example/library/tar.gz/{sha}"
    write_json(directory / "source.receipt.json", {"url": url, "effective_url": url, "http_status": 200, "returncode": 0, "body": ref(path, tmp_path)})
    tree = {"sha": "b" * 40, "truncated": False, "tree": [
        {"path": name, "mode": "100644", "type": "blob", "sha": blob_sha(body), "size": len(body)}
        for name, body in contents.items()
    ]}
    commit = {"sha": sha, "tree": {"sha": tree["sha"]}}
    monkeypatch.setattr("scripts.benchmark_expansion_sources.capture", lambda endpoint, *args: ({}, commit if "/git/commits/" in endpoint else tree))
    return {"repo": "example/library", "sha": sha}, path


def test_exact_source_match_is_not_task_admission(tmp_path, monkeypatch):
    task, _ = fixture(tmp_path, monkeypatch)
    row = freeze_version(task, tmp_path)
    assert row["status"] == "java_content_verified"
    assert row["full_repository_blob_match"] is True
    assert row["task_admission"] == "not_implied"


def test_export_omitted_java_prevents_size_verification(tmp_path, monkeypatch):
    task, _ = fixture(tmp_path, monkeypatch, omit_java=True)
    row = freeze_version(task, tmp_path)
    assert row["status"] == "conflict"
    assert row["tree_comparison"]["missing_java_paths"] == ["src/main/java/Example.java"]


def test_java_size_does_not_claim_complete_checkout(tmp_path, monkeypatch):
    task, _ = fixture(tmp_path, monkeypatch, omit_other=True)
    row = freeze_version(task, tmp_path)
    assert row["status"] == "java_content_verified"
    assert row["full_repository_blob_match"] is False


def test_changed_archive_is_not_reused(tmp_path, monkeypatch):
    task, path = fixture(tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes() + b"changed")
    assert freeze_version(task, tmp_path)["status"] == "unavailable"
