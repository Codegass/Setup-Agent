import base64

import pytest

from scripts import benchmark_eclipse_source_review as review
from scripts.benchmark_source_audit import blob_sha


@pytest.mark.parametrize(
    "path", ["../pom.xml", "/pom.xml", "x/../pom.xml", "x//pom.xml", "x\\pom.xml", "."]
)
def test_paths_are_not_allowed_to_escape_or_alias(path):
    with pytest.raises(ValueError):
        review.safe_source_path(path)


def responses(monkeypatch, tmp_path, *, mode="100644", corrupt=False, tree_identity=None):
    raw = b"<project/>\n"
    blob = blob_sha(raw)
    commit, tree = "a" * 40, "b" * 40

    def capture(endpoint, *args):
        if "/commits/" in endpoint:
            return {}, {"sha": commit, "tree": {"sha": tree}}
        if "/trees/" in endpoint:
            return {}, {
                "sha": tree_identity or tree,
                "truncated": False,
                "tree": [
                    {"path": "pom.xml", "type": "blob", "mode": mode, "sha": blob, "size": len(raw)}
                ],
            }
        assert "/blobs/" in endpoint
        return {}, {
            "sha": blob,
            "encoding": "base64",
            "content": base64.b64encode(raw + (b"bad" if corrupt else b"")).decode(),
            "size": len(raw),
        }

    monkeypatch.setattr(review, "capture", capture)
    return review.Source(tmp_path, "official/project", commit)


def test_only_verified_regular_bytes_are_archived(monkeypatch, tmp_path):
    source = responses(monkeypatch, tmp_path)
    item = source.file("pom.xml")
    assert item["status"] == "verified"
    assert (tmp_path / item["file"]["path"]).read_bytes() == b"<project/>\n"
    assert source.file("build.gradle")["status"] == "absent"


def test_symlink_not_followed(monkeypatch, tmp_path):
    source = responses(monkeypatch, tmp_path, mode="120000")
    assert source.file("pom.xml")["status"] == "not_regular_file"
    assert not (tmp_path / "objects").exists()


def test_wrong_blob_bytes_rejected(monkeypatch, tmp_path):
    source = responses(monkeypatch, tmp_path, corrupt=True)
    with pytest.raises(ValueError, match="pinned Git blob"):
        source.file("pom.xml")


def test_tree_identity_rejected(monkeypatch, tmp_path):
    source = responses(monkeypatch, tmp_path, tree_identity="c" * 40)
    with pytest.raises(ValueError, match="Tree"):
        source.file("pom.xml")
