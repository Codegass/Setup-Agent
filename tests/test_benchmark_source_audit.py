"""Offline boundary tests for source evidence and continuous size measurement."""

import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from scripts.benchmark_source_audit import (
    audit_task,
    blob_sha,
    compare_tree,
    descriptor,
    member_path,
    scan_archive,
    verified_tree,
)


def archive_at(path, entries):
    with tarfile.open(path, "w:gz") as stream:
        for name, data, kind in entries:
            row = tarfile.TarInfo(name)
            if kind == "symlink":
                row.type = tarfile.SYMTYPE
                row.linkname = data
                stream.addfile(row)
            else:
                row.size = len(data)
                stream.addfile(row, io.BytesIO(data))


def test_stream_measurement_lf_lines_and_no_checkout_extraction(tmp_path):
    archive = tmp_path / "source.tar.gz"
    source = b"a\r\nb\nc\r"
    archive_at(
        archive,
        [
            ("repo/src/test/A.java", source, "file"),
            ("repo/E.java", b"", "file"),
            (
                "repo/pom.xml",
                b"<project><build><defaultGoal>clean verify</defaultGoal></build></project>",
                "file",
            ),
        ],
    )
    out = tmp_path / "audit"
    result = scan_archive(archive, {"pom.xml"}, out)
    assert result["path_safe"] is True
    assert result["measured_size"] == {
        "java_files": 2,
        "java_physical_lines": 3,
        "java_source_bytes": len(source),
    }
    assert result["size_by_path_convention"]["java_lines_by_path"]["test"] == 3
    inventory = json.loads((out / result["inventory"]["path"]).read_bytes())["entries"]
    assert inventory["src/test/A.java"]["git_blob_sha1"] == blob_sha(source)
    assert result["descriptors"][0]["default_goal"] == "clean verify"
    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside.java",
        "repo/../A.java",
        "/repo/A.java",
        "C:/repo/A.java",
        "repo//A.java",
        "repo\\A.java",
    ],
)
def test_reject_unsafe_archive_paths(name):
    with pytest.raises(ValueError):
        member_path(name)


def test_duplicate_and_escape_symlink_cannot_be_safe(tmp_path):
    path = tmp_path / "bad.tar.gz"
    archive_at(
        path,
        [
            ("repo/A.java", b"a", "file"),
            ("repo/A.java", b"b", "file"),
            ("repo/escape", "../outside", "symlink"),
        ],
    )
    result = scan_archive(path, set(), tmp_path / "audit")
    assert result["path_safe"] is False
    assert {x["code"] for x in result["issues"]} >= {
        "duplicate_archive_member",
        "unsafe_symlink_target",
    }


def test_same_java_paths_wrong_bytes_are_not_verified():
    tree = {
        "tree": [
            {"path": "A.java", "type": "blob", "mode": "100644", "sha": blob_sha(b"a"), "size": 1}
        ]
    }
    entries = {"A.java": {"type": "file", "git_blob_sha1": blob_sha(b"b"), "bytes": 1}}
    result = compare_tree(entries, tree)
    assert result["java_content_verified"] is False
    assert result["java_content_mismatches"] == ["A.java"]


def test_symlinks_and_gitlinks_are_not_invented_java_source():
    tree = {
        "tree": [
            {
                "path": "A.java",
                "type": "blob",
                "mode": "120000",
                "sha": blob_sha(b"src/A.java"),
                "size": 10,
            },
            {"path": "module", "type": "commit", "mode": "160000", "sha": "b" * 40},
        ]
    }
    result = compare_tree({}, tree)
    assert result["tracked_regular_java_files"] == 0
    assert result["java_symlink_paths"] == ["A.java"]
    assert result["gitlinks"][0]["path"] == "module"


def make_tree(base, sha):
    tree = base / "tree.json"
    url = f"https://api.github.com/repos/org/repo/git/trees/{sha}"
    tree.write_text(json.dumps({"sha": sha, "url": url, "truncated": False, "tree": []}))
    data = tree.read_bytes()
    meta = {
        "url": url + "?recursive=1",
        "returncode": 0,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (base / "tree.json.meta.json").write_text(json.dumps(meta))
    return tree


def test_tree_request_exact_sha_and_raw_hash_required(tmp_path):
    path = make_tree(tmp_path, "a" * 40)
    assert verified_tree(path, tmp_path, "org/repo", "a" * 40)[0]["valid"] is True
    assert verified_tree(path, tmp_path, "org/repo", "b" * 40)[0]["valid"] is False
    path.write_bytes(path.read_bytes() + b" ")
    assert verified_tree(path, tmp_path, "org/repo", "a" * 40)[0]["valid"] is False


@pytest.mark.parametrize("receipt", ["suffix", "directory", "wrong_directory"])
def test_missing_tree_cannot_become_complete_from_matching_totals(tmp_path, receipt):
    base = tmp_path / "dataset"
    base.mkdir()
    out = tmp_path / "out"
    archive = base / "source.tar.gz"
    archive_at(archive, [("repo/A.java", b"a\n", "file")])
    scan = scan_archive(archive, set(), out)
    sha = "a" * 40
    (base / "source.tar.gz.meta.json").write_text(
        json.dumps(
            {
                "url": f"https://codeload.github.com/org/repo/tar.gz/{sha}",
                "sha256": scan["sha256"],
                "bytes": scan["bytes"],
            }
        )
    )
    (base / "review.json").write_text(
        json.dumps({"repo": "org/repo", "sha": sha, "archive_sha256": scan["sha256"]})
    )
    task = {
        "task_id": "task",
        "repo": "org/repo",
        "sha": sha,
        "source_archive": "source.tar.gz",
        "source_archive_sha256": scan["sha256"],
        "source_review_file": "review.json",
        "source_size": scan["measured_size"],
    }
    if receipt != "suffix":
        old = base / "source.tar.gz.meta.json"
        metadata = json.loads(old.read_bytes())
        metadata.update(archive="source.tar.gz", sha=sha if receipt == "directory" else "b" * 40)
        (base / "archive-download.json").write_text(json.dumps(metadata))
        old.unlink()
    result = audit_task(task, True, scan, base, out, {})
    assert result["source_completeness"] == (
        "unavailable" if receipt == "wrong_directory" else "internal_consistency_only"
    )
    assert all(row["equal"] for row in result["old_size_comparison"].values())


def test_descriptor_literal_only_and_no_entities():
    value = descriptor(
        b"<project><packaging>pom</packaging><profiles><profile><build><defaultGoal>install</defaultGoal></build></profile></profiles></project>"
    )
    assert value["packaging"] == "pom"
    assert value["default_goal"] is None
    assert "parse_error" in descriptor(
        b'<!DOCTYPE project [<!ENTITY x "verify">]><project>&x;</project>'
    )
