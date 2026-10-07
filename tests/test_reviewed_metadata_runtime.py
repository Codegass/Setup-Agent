"""Exercise runtime admission through the complete offline review importer.

The archived DbUtils plan supplies the surrounding validation fixture only.
Task/request/runtime mutations below are synthetic parser cases, not new
claims about which JDK produced the historical project's effective model.
"""

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.build_benchmark_requirements import apply_reviewed_plan, task_digest


def runtime_review(tmp_path, java_major, runtime_text):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/requirements_reviewed_metadata.zip") as archive:
        prefix = "commons-dbutils/"
        draft, task, review = [json.loads(archive.read(prefix + name + ".json"))
                               for name in ("draft", "task", "review")]
        for source in review["sources"].values():
            target = tmp_path / source["path"]
            assert target.resolve().is_relative_to(tmp_path.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(prefix + source["path"]))

    def replace_source(name, raw):
        source = review["sources"][name]
        (tmp_path / source["path"]).write_bytes(raw)
        source.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))

    task["steps"][0]["java_major"] = java_major
    draft["task_sha256"] = review["task_sha256"] = task_digest(task)
    request = json.loads((tmp_path / review["sources"]["preparation_request"]["path"]).read_text())
    request["original_task"]["java_major"] = java_major
    replace_source("preparation_request", json.dumps(request).encode())
    replace_source("runtime", runtime_text.encode())
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review))
    return draft, task, path


@pytest.mark.parametrize("major,version", [
    (8, "1.8.0_504, vendor: Temurin"),
    (8, "8.0.504, vendor: Temurin"),
    (17, "17.0.12, vendor: Eclipse Adoptium"),
    (21, "21.0.12.1, vendor: Eclipse Adoptium"),
    (21, "21"),
])
def test_matching_legacy_and_current_java_major_imports(tmp_path, major, version):
    draft, task, path = runtime_review(
        tmp_path, major, "\x1b[1mApache Maven 3.9.16\x1b[0m\nJava version: " + version)
    result = apply_reviewed_plan(draft, task, path, tmp_path / "result")
    assert result["annotation_completeness"]["status"] == "complete"
    assert result["task_sha256"] == task_digest(task)


@pytest.mark.parametrize("major,version,maven", [
    (8, "17.0.12", "3.9.16"),
    (17, "1.8.0_504", "3.9.16"),
    (8, "18.0.2", "3.9.16"),
    (21, "121.0.1", "3.9.16"),
    (8, "unavailable", "3.9.16"),
    (8, "", "3.9.16"),
    (8, "8garbage", "3.9.16"),
    (8, "1.8.0_504", "3.9.9"),
    (8, "1.8.0_504", "3.9.160"),
])
def test_mismatching_or_unobserved_runtime_still_rejects(tmp_path, major, version, maven):
    draft, task, path = runtime_review(
        tmp_path, major, f"Apache Maven {maven}\nJava version: {version}\n")
    with pytest.raises(ValueError, match="Metadata preparation runtime differs"):
        apply_reviewed_plan(draft, task, path, tmp_path / "result")


def test_matching_version_cannot_bypass_runtime_byte_binding(tmp_path):
    draft, task, path = runtime_review(
        tmp_path, 8, "Apache Maven 3.9.16\nJava version: 1.8.0_504\n")
    review = json.loads(path.read_text())
    (tmp_path / review["sources"]["runtime"]["path"]).write_text(
        "Apache Maven 3.9.16\nJava version: 8.0.504\n")
    with pytest.raises(ValueError, match="byte hash mismatch"):
        apply_reviewed_plan(draft, task, path, tmp_path / "result")
