"""Independent negative tests for the reviewed generated-state exception."""

from copy import deepcopy
import json

import pytest

from sag.benchmark.recorder import reference
from sag.benchmark.wrapper_review import (
    DEVELOCITY_STATE,
    capture_request,
    capture_wrapper_inputs,
    snapshot_inputs,
    validate_launcher_review,
)
from test_benchmark_wrapper_review import fixture
from test_wrapper_runtime_state import extension, state


def snapshot(fixture):
    root, task, spec, env, _ = fixture
    step = task["steps"][0]
    review = spec["steps"][0]["launcher_review"]
    request = capture_request(task, step, review, root, "r1", "i1", "acceptance_before", env)
    return capture_wrapper_inputs(request)


@pytest.mark.parametrize("mutation", ["directory", "producer", "codec", "source"])
def test_runtime_exception_profile_cannot_be_broadened(fixture, mutation):
    extension(fixture)
    _, task, spec, _, _ = fixture
    review = deepcopy(spec["steps"][0]["launcher_review"])
    rule = review["runtime_state"][0]
    if mutation == "directory":
        rule["path"] = ".mvn/.develocity"
    elif mutation == "producer":
        rule["producer"]["version"] = "2.6.0"
    elif mutation == "codec":
        rule["format"] = "any_bytes"
    else:
        rule["reviewed_sources"]["launcher_state_capture_class"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Unreviewed runtime state"):
        validate_launcher_review(review, task["sha"], task["steps"][0])


@pytest.mark.parametrize("mutation", ["remove", "invent_absence", "replace_bytes", "duplicate", "extra_path"])
def test_state_raw_observation_cannot_be_deleted_or_reinterpreted(fixture, mutation):
    extension(fixture)
    root, task, spec, _, _ = fixture
    state(root, b"a" * 26)
    observed = snapshot(fixture)
    step = task["steps"][0]
    review = spec["steps"][0]["launcher_review"]
    assert snapshot_inputs(review, task, step, observed, "r1", "i1", "acceptance_before")["serial"]
    if mutation == "remove":
        observed["runtime_state"] = []
    elif mutation == "invent_absence":
        observed["runtime_state"][0]["present"] = False
    elif mutation == "replace_bytes":
        observed["runtime_state"][0]["data"] = "Yg=="
    elif mutation == "duplicate":
        observed["runtime_state"].append(deepcopy(observed["runtime_state"][0]))
    else:
        observed["current_paths"].append(".mvn/.develocity/options")
    with pytest.raises(ValueError):
        snapshot_inputs(review, task, step, observed, "r1", "i1", "acceptance_before")


@pytest.mark.parametrize("mutation", ["missing", "wrong_hash", "tampered_bytes"])
def test_import_requires_exact_reviewed_producer_bytes(fixture, tmp_path, mutation):
    from scripts.build_benchmark_requirements import import_launcher_review

    extension(fixture)
    root, task, spec, _, _ = fixture
    review = deepcopy(spec["steps"][0]["launcher_review"])
    entries = [
        {"source_path": item["path"], "kind": "file", "pinned_bytes_equal": True,
         **reference(root, root / item["path"])}
        for item in review["files"]
    ]
    index = tmp_path / "inventory.json"
    index.write_text(json.dumps({"commit": task["sha"], "files": entries}))
    sources = {"launcher_inventory": reference(tmp_path, index)}
    sources.update({"launcher_source:" + item["source_path"]:
                    reference(tmp_path, root / item["source_path"]) for item in entries})
    if mutation != "missing":
        fake = tmp_path / "producer.jar"
        fake.write_bytes(b"not the reviewed producer")
        claimed = {**reference(tmp_path, fake),
                   **DEVELOCITY_STATE["reviewed_sources"]["launcher_state_producer_jar"]}
        if mutation == "wrong_hash":
            claimed["sha256"] = "0" * 64
        sources["launcher_state_producer_jar"] = claimed
    with pytest.raises(ValueError):
        import_launcher_review({"launcher_review": review, "additional_sources": sources},
                               tmp_path, task, task["steps"][0], tmp_path / "out")


@pytest.mark.parametrize("generated_state", [False, True])
def test_old_review_import_preserves_strict_closure_without_new_exemption(
    fixture, tmp_path, generated_state
):
    from scripts.build_benchmark_requirements import import_launcher_review

    extension(fixture)
    root, task, spec, env, _ = fixture
    old_review = deepcopy(spec["steps"][0]["launcher_review"])
    del old_review["runtime_state"]
    entries = [
        {"source_path": item["path"], "kind": "file", "pinned_bytes_equal": True,
         **reference(root, root / item["path"])}
        for item in old_review["files"]
    ]
    index = tmp_path / "old-inventory.json"
    index.write_text(json.dumps({"commit": task["sha"], "files": entries}))
    sources = {"launcher_inventory": reference(tmp_path, index)}
    sources.update({"launcher_source:" + item["source_path"]:
                    reference(tmp_path, root / item["source_path"]) for item in entries})
    imported, archived = import_launcher_review(
        {"launcher_review": old_review, "additional_sources": sources},
        tmp_path, task, task["steps"][0], tmp_path / "out",
    )
    assert imported == old_review
    assert "runtime_state" not in imported
    assert not any(name.startswith("launcher_state_") for name in archived)
    if generated_state:
        state(root, b"a" * 26)
    step = task["steps"][0]
    request = capture_request(task, step, imported, root, "r1", "i1", "acceptance_before", env)
    observed = capture_wrapper_inputs(request)
    if generated_state:
        with pytest.raises(ValueError, match="inventory"):
            snapshot_inputs(imported, task, step, observed, "r1", "i1", "acceptance_before")
    else:
        assert snapshot_inputs(imported, task, step, observed, "r1", "i1", "acceptance_before")["serial"]
