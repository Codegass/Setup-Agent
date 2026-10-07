"""Independent checks of official transport provenance and frozen acquisition scope."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import benchmark_checkout_recovery as checkout


def test_network_capture_explicitly_selects_official_host(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            stdout=b'HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n{"sha":"a"}',
            stderr=b"",
            returncode=0,
        )

    monkeypatch.setenv("GH_HOST", "unrelated.example")
    monkeypatch.setattr(checkout.subprocess, "run", fake_run)
    checkout.fetch("repos/org/repo/git/commits/" + "a" * 40, tmp_path / "response.json", True)
    argv, _ = calls[0]
    assert "--hostname" in argv
    assert argv[argv.index("--hostname") + 1] == "github.com"


@pytest.mark.parametrize("damage", ["missing", "hash_mismatch", "contradictory_status"])
def test_cached_transport_headers_are_part_of_the_proof(tmp_path, damage):
    path = tmp_path / "response.json"
    raw = b'{"sha":"a"}'
    path.write_bytes(raw)
    headers = b"HTTP/2.0 200 OK\r\ncontent-type: application/json"
    header_path = path.with_suffix(".json.headers.txt")
    header_path.write_bytes(headers)
    meta = {
        "url": "https://api.github.com/repos/org/repo/git/commits/" + "a" * 40,
        "http_status": 200,
        "returncode": 0,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "response_headers_sha256": hashlib.sha256(headers).hexdigest(),
    }
    if damage == "missing":
        header_path.unlink()
    elif damage == "hash_mismatch":
        header_path.write_bytes(headers + b"\nextra: changed")
    else:
        changed = headers.replace(b"200 OK", b"404 Not Found")
        header_path.write_bytes(changed)
        meta["response_headers_sha256"] = hashlib.sha256(changed).hexdigest()
    path.with_suffix(".json.meta.json").write_text(json.dumps(meta))
    with pytest.raises((ValueError, OSError)):
        checkout.fetch("repos/org/repo/git/commits/" + "a" * 40, path, False)


def test_queue_cannot_be_joined_to_another_source_dataset(tmp_path):
    previous = tmp_path / "previous"
    original = tmp_path / "old"
    original.mkdir()
    (original / "dataset.json").write_bytes(b"{}")
    expected = hashlib.sha256(b"{}").hexdigest()
    (previous / "source-audit").mkdir(parents=True)
    (previous / "source-audit/audit.json").write_text(
        json.dumps({"dataset_ref": {"sha256": expected}, "tasks": []})
    )
    (previous / "COLLECTION_QUEUE.json").write_text(
        json.dumps({"source_dataset_sha256": "b" * 64, "tasks": []})
    )
    with pytest.raises(ValueError):
        checkout.audit(original, previous, tmp_path / "new", network=False)
