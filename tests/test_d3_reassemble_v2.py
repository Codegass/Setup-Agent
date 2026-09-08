import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts import d3_reassemble_v2 as driver


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checksums(folder: Path) -> None:
    (folder / "SHA256SUMS").write_text(
        "".join(
            f"{_digest(path)}  {path.name}\n"
            for path in sorted(folder.iterdir())
            if path.is_file() and path.name != "SHA256SUMS"
        )
    )


@pytest.fixture
def freeze(tmp_path):
    seat = tmp_path / "demo"
    seat.mkdir()
    original = {
        "schema_version": 1,
        "repo": "apache/demo",
        "sha": "a" * 40,
        "matched_cell": "build (17)",
        "cells": [],
    }
    (seat / "target_record.json").write_text(json.dumps(original))
    (seat / "run-jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "build (17)", "conclusion": "success", "status": "completed"},
                ]
            }
        )
    )
    pin = {
        "repo": original["repo"],
        "sha": original["sha"],
        "target_sha256": _digest(seat / "target_record.json"),
    }
    (tmp_path / "d3-pin-manifest.json").write_text(json.dumps({"seats": {"demo": pin}}))
    _checksums(seat)
    _checksums(tmp_path)
    return tmp_path


def test_reassembly_preserves_originals_and_refreshes_only_generated_digests(freeze):
    original = (freeze / "demo/target_record.json").read_bytes()
    manifest = (freeze / "d3-pin-manifest.json").read_bytes()
    original_sums = (freeze / "demo/SHA256SUMS").read_text()
    assert driver.main(["--freeze", str(freeze)]) == 0
    assert driver.main(["--freeze", str(freeze)]) == 0
    assert (freeze / "demo/target_record.json").read_bytes() == original
    assert (freeze / "d3-pin-manifest.json").read_bytes() == manifest
    assert (freeze / "demo/SHA256SUMS").read_text().startswith(original_sums)
    for folder in (freeze, freeze / "demo"):
        for line in (folder / "SHA256SUMS").read_text().splitlines():
            digest, path = line.split(maxsplit=1)
            assert _digest(folder / path) == digest


@pytest.mark.parametrize("relative", ["demo/target_record.json", "demo/run-jobs.json"])
def test_changed_frozen_inputs_are_rejected_before_any_refetch(freeze, relative, monkeypatch):
    (freeze / relative).write_text("{}")
    monkeypatch.setattr(driver, "fetch_logs", lambda *_: pytest.fail("must validate first"))
    assert driver.main(["--freeze", str(freeze), "--refetch"]) == 1
    assert not (freeze / "demo/target_record.v2.json").exists()


def test_manifest_digest_must_match_original_record_even_with_new_checksums(freeze):
    path = freeze / "d3-pin-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["seats"]["demo"]["target_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    _checksums(freeze)
    assert driver.main(["--freeze", str(freeze)]) == 1
    assert not (freeze / "demo/target_record.v2.json").exists()


def test_changed_cell_does_not_replace_an_existing_reassembled_record(freeze, monkeypatch):
    assert driver.main(["--freeze", str(freeze)]) == 0
    previous = (freeze / "demo/target_record.v2.json").read_bytes()
    harvest = driver.harvest_from_dir

    def changed_cell(*args, **kwargs):
        record, digest, summary = harvest(*args, **kwargs)
        return record.model_copy(update={"matched_cell": None}), digest, summary

    monkeypatch.setattr(driver, "harvest_from_dir", changed_cell)
    assert driver.main(["--freeze", str(freeze)]) == 1
    assert (freeze / "demo/target_record.v2.json").read_bytes() == previous
    assert not (freeze / "demo/.target_record.v2.pending.json").exists()


def test_late_courier_failure_still_records_completed_seats_and_their_checksums(
    freeze, monkeypatch
):
    shutil.copytree(freeze / "demo", freeze / "second")
    manifest_path = freeze / "d3-pin-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["seats"]["second"] = manifest["seats"]["demo"]
    manifest_path.write_text(json.dumps(manifest))
    _checksums(freeze)
    assert driver.main(["--freeze", str(freeze)]) == 0

    def fetch_sources(_repo, _sha, snapshot):
        if snapshot.name == "second":
            raise driver.HarvestError("pinned source mismatch")
        return ()

    monkeypatch.setattr(driver, "fetch_logs", lambda *_: ())
    monkeypatch.setattr(driver, "fetch_sources", fetch_sources)
    assert driver.main(["--freeze", str(freeze), "--refetch"]) == 1
    addendum = json.loads((freeze / "d3-pin-manifest-v2-addendum.json").read_text())
    assert addendum["seats"]["demo"]["record_v2_sha256"] == _digest(
        freeze / "demo/target_record.v2.json"
    )
    assert "pinned source mismatch" in addendum["seats"]["second"]["error"]
    for folder in (freeze, freeze / "demo", freeze / "second"):
        for line in (folder / "SHA256SUMS").read_text().splitlines():
            digest, path = line.split(maxsplit=1)
            assert _digest(folder / path) == digest
