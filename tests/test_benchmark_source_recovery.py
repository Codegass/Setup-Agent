"""Fixed source acquisition scope and exact Git object integrity, without network."""

import base64
from copy import deepcopy
import json

import pytest

from scripts import benchmark_source_recovery as recovery


def test_commit_tree_link_and_complete_response_required():
    commit = {"sha": "a" * 40, "tree": {"sha": "b" * 40}}
    tree = {"sha": "b" * 40, "truncated": False, "tree": []}
    recovery.validate_tree(commit, tree, "a" * 40)
    for key, value in [("sha", "c" * 40), ("truncated", True)]:
        bad = {**tree, key: value}
        with pytest.raises(ValueError):
            recovery.validate_tree(commit, bad, "a" * 40)
    with pytest.raises(ValueError):
        recovery.validate_tree(commit, tree, "d" * 40)


@pytest.mark.parametrize(
    "path", ["../A.java", "/A.java", "src/../A.java", "src//A.java", "src\\A.java"]
)
def test_unsafe_git_path_is_rejected(path):
    with pytest.raises(ValueError):
        recovery.validate_tree(
            {"sha": "a" * 40, "tree": {"sha": "b" * 40}},
            {"sha": "b" * 40, "truncated": False, "tree": [{"path": path}]},
            "a" * 40,
        )


def test_git_blob_content_hash_not_only_identity_string():
    raw = b"public class A {}\n"
    sha = recovery.blob_sha(raw)
    row = {"sha": sha, "size": len(raw)}
    payload = {
        "sha": sha,
        "size": len(raw),
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    }
    assert recovery.decode_blob(payload, row) == raw
    payload["content"] = base64.b64encode(raw.replace(b"A", b"B")).decode()
    with pytest.raises(ValueError, match="hash/size"):
        recovery.decode_blob(payload, row)


def recovery_fixture(tmp_path, monkeypatch, frozen, corrupt_original=False):
    old_base, out = tmp_path / "old", tmp_path / "new"
    old_base.mkdir()
    out.mkdir()
    original = old_base / "original.tar.gz"
    original.write_bytes(b"original archive fixture")
    raw_a, raw_b = b"a\n", b"b\nc"
    sha_a, sha_b = recovery.blob_sha(raw_a), recovery.blob_sha(raw_b)
    inv = old_base / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "entries": {
                    "A.java": {
                        "type": "file",
                        "git_blob_sha1": sha_a,
                        "bytes": len(raw_a),
                        "physical_lines": 1,
                    }
                }
            }
        )
    )
    task = {
        "task_id": "task",
        "repo": "org/repo",
        "sha": "a" * 40,
        "is_canonical": True,
        "old_inventory": recovery.file_ref(inv, old_base),
        "before_source_completeness": "conflict",
        "before_size": {"java_files": 1, "java_physical_lines": 1, "java_source_bytes": 2},
        "frozen_missing_java_paths": frozen,
        "original_refs": {
            "source_archive": original.name,
            "source_archive_sha256": recovery.digest(original),
        },
    }
    tree = {
        "sha": "b" * 40,
        "truncated": False,
        "tree": [
            {"path": "A.java", "type": "blob", "mode": "100644", "sha": sha_a, "size": len(raw_a)},
            {"path": "B.java", "type": "blob", "mode": "100644", "sha": sha_b, "size": len(raw_b)},
        ],
    }
    calls = []

    def fake_capture(endpoint, *_args):
        calls.append(endpoint)
        if "/commits/" in endpoint:
            return {}, {"sha": "a" * 40, "tree": {"sha": "b" * 40}}
        if "/trees/" in endpoint:
            return {}, deepcopy(tree)
        assert endpoint.endswith(sha_b)
        return {}, {
            "sha": sha_b,
            "size": len(raw_b),
            "encoding": "base64",
            "content": base64.b64encode(raw_b).decode(),
        }

    monkeypatch.setattr(recovery, "capture", fake_capture)
    before = inv.read_bytes()
    if corrupt_original:
        original.write_bytes(b"mutated old archive")
    result = recovery.recover_task(
        task, {"dataset_base": str(old_base)}, {"old_audit_base": str(old_base)}, out, True
    )
    assert inv.read_bytes() == before
    return result, calls, out


def test_only_frozen_missing_java_is_materialized_as_separate_overlay(tmp_path, monkeypatch):
    result, calls, out = recovery_fixture(tmp_path, monkeypatch, ["B.java"])
    assert result["after_source_completeness"] == "java_content_verified"
    assert result["after_size"] == {
        "java_files": 2,
        "java_physical_lines": 3,
        "java_source_bytes": 5,
    }
    assert result["evidence_layout"] == "original_archive_plus_verified_java_overlay"
    assert result["original_archive_rewritten"] is False
    assert result["campaign_ready"] is False
    assert len(calls) == 3
    assert (out / result["overlay"][0]["source"]["path"]).read_bytes() == b"b\nc"


def test_new_unlisted_gap_is_reported_without_scope_expansion(tmp_path, monkeypatch):
    result, calls, _ = recovery_fixture(tmp_path, monkeypatch, [])
    assert result["after_source_completeness"] == "conflict"
    assert result["overlay"] == []
    assert len(calls) == 2
    assert result["after_tree_comparison"]["missing_java_paths"] == ["B.java"]


def test_mutated_original_archive_cannot_use_a_stale_inventory(tmp_path, monkeypatch):
    result, calls, _ = recovery_fixture(tmp_path, monkeypatch, ["B.java"], corrupt_original=True)
    assert result["after_source_completeness"] == "unavailable"
    assert calls == []
    assert "Original compressed archive changed" in result["errors"][0]


def test_cached_response_must_match_raw_http_without_network(tmp_path):
    root = tmp_path / "capture"
    attempt = root / "request/attempt-001"
    attempt.mkdir(parents=True)
    body = b'{"sha":"abc"}'
    (attempt / "body.json").write_bytes(body)
    (attempt / "response.http").write_bytes(
        b"HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n" + body
    )
    (attempt / "stderr.txt").write_bytes(b"")
    receipt = {
        "url": "https://api.github.com/repos/org/repo/git/commits/abc",
        "method": "GET",
        "returncode": 0,
        "http_status": 200,
        "body": recovery.file_ref(attempt / "body.json", root),
        "raw_response": recovery.file_ref(attempt / "response.http", root),
        "stderr": recovery.file_ref(attempt / "stderr.txt", root),
    }
    recovery.write_json(attempt / "receipt.json", receipt)
    assert (
        recovery.capture("repos/org/repo/git/commits/abc", root / "request", root, False)[1]["sha"]
        == "abc"
    )
    (attempt / "body.json").write_bytes(b'{"sha":"different"}')
    receipt["body"] = recovery.file_ref(attempt / "body.json", root)
    recovery.write_json(attempt / "receipt.json", receipt)
    with pytest.raises(ValueError, match="contradicts"):
        recovery.capture("repos/org/repo/git/commits/abc", root / "request", root, False)
