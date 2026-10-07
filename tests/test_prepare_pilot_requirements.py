"""Pilot preparation is inert, revision-bound and preserves command scope."""
import io
import json
from pathlib import Path
import tarfile

import pytest

from scripts.prepare_pilot_requirements import (
    blob_hash, capture_configs, file_ref, metadata_requests, pom_declarations,
    recovery_reference, runtime_observations, selected_commands,
)


def test_two_commands_keep_upstream_skips_in_first_step_only(tmp_path):
    task = {"task_id": "one", "repo": "example/tiny", "sha": "a" * 40, "tool": "maven",
            "environment_and_commands": {"commands": [
                {"text": "mvn -f pom.xml -Pci install -DskipTests=true -Dmaven.javadoc.skip=true -B -V"},
                {"text": "mvn test -B"}], "official_commands": ["not a third command"]}}
    commands = selected_commands(task)
    request = metadata_requests(task, commands, {}, tmp_path)
    assert len(request["requests"]) == 2
    first = request["requests"][0]["proposed_metadata_probes"][0]["argv"]
    second = request["requests"][1]["proposed_metadata_probes"][0]["argv"]
    assert first[:4] == ["mvn", "-f", "pom.xml", "-Pci"]
    assert "-DskipTests=true" in first and "-Dmaven.javadoc.skip=true" in first
    assert not any(x in {"install", "test", "verify"} for x in first + second)
    assert not any("skipTests" in x for x in second)
    assert request["execution_policy"]["state"].startswith("requests only")


def test_shell_commands_do_not_silently_become_literal_frozen_tasks():
    with pytest.raises(ValueError, match="shell expansion"):
        selected_commands({"environment_and_commands": {"command": "mvn verify && mvn install"}})


def test_conditional_profile_is_not_an_active_module_or_mandatory_jar():
    raw = b"""<project><packaging>pom</packaging><modules><module>core</module></modules>
<profiles><profile><id>newjdk</id><activation><jdk>[17,)</jdk></activation>
<modules><module>eclipse_plugin</module></modules></profile></profiles></project>"""
    declaration = pom_declarations(raw)
    assert declaration["declared_modules"] == ["core"]
    assert declaration["profiles"][0]["modules"] == ["eclipse_plugin"]
    assert "unresolved" in declaration["interpretation"]
    assert "artifacts" not in declaration


def test_runtime_input_observation_ignores_unrelated_secrets():
    observed = runtime_observations([{"text": """2026-09-16T00:00:00Z   MAVEN_ARGS: --show-version --batch-mode
2026-09-16T00:00:01Z   SECRET_TOKEN: example-must-not-copy
2026-09-16T00:00:02Z Java version: 21.0.12.1, vendor: Eclipse Adoptium
"""}])
    assert observed["environment_values"][0]["name"] == "MAVEN_ARGS"
    assert "example-must-not-copy" not in json.dumps(observed)
    assert observed["tool_version_lines"][0]["line"] == 3


def archive_fixture(tmp_path, *, bytes_match=True):
    sha = "a" * 40
    raw = b"<project><artifactId>tiny</artifactId></project>"
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        entry = tarfile.TarInfo("tiny-" + sha + "/pom.xml")
        entry.size = len(raw)
        out.addfile(entry, io.BytesIO(raw))
    tree = {"sha": sha, "truncated": False, "tree": [{"path": "pom.xml", "type": "blob", "mode": "100644",
              "sha": blob_hash(raw if bytes_match else raw + b"\n")} ]}
    (tmp_path / "tree.json").write_text(json.dumps(tree))
    task = {"task_id": "one", "repo": "example/tiny", "sha": sha, "source_archive": "source.tar.gz",
            "source_archive_sha256": file_ref(str(archive))["sha256"]}
    audit = {"tasks": [{"task_id": "one", "repo": task["repo"], "sha": sha,
                        "tree_binding": {"valid": True, "response_sha": sha, "path": "tree.json",
                                         "sha256": file_ref(str(tmp_path / "tree.json"))["sha256"]}}]}
    return task, audit


def test_changed_archive_config_bytes_remain_a_recorded_gap(tmp_path):
    task, audit = archive_fixture(tmp_path, bytes_match=False)
    result = capture_configs(tmp_path, task, audit, tmp_path / "out")
    assert result["config_byte_mismatches"] == ["pom.xml"]
    assert result["all_selected_config_bytes_match_pinned_blobs"] is False
    assert result["files"][0]["pinned_blob_verified"] is False


def test_other_revision_tree_cannot_supply_configuration_identity(tmp_path):
    task, audit = archive_fixture(tmp_path)
    audit["tasks"][0]["tree_binding"]["response_sha"] = "b" * 40
    with pytest.raises(ValueError, match="exact complete Git tree"):
        capture_configs(tmp_path, task, audit, tmp_path / "out")


def test_exact_blob_recovery_keeps_archive_bytes_and_does_not_upgrade_readiness(tmp_path):
    original, restored = b"echo example\r\n", b"echo example\n"
    (tmp_path / "original.bat").write_bytes(original)
    (tmp_path / "restored.bat").write_bytes(restored)
    old = {**file_ref(str(tmp_path / "original.bat")), "source_path": "gradlew.bat",
           "git_blob_sha1": blob_hash(restored), "pinned_blob_verified": False}
    recovered = {"repository_path": "gradlew.bat", "git_blob_sha": blob_hash(restored),
                 "original_archived_sha256": old["sha256"],
                 "file": {**file_ref(str(tmp_path / "restored.bat")), "path": "restored.bat"}}
    task = {"repo": "example/tiny", "sha": "a" * 40}
    audit = {"evidence_base": str(tmp_path), "revisions": [{**task, "recovered_blobs": [recovered]}]}
    path = tmp_path / "recovery.json"
    path.write_text(json.dumps(audit))
    result = recovery_reference(task, {"files": [old]}, path)
    assert result["checkout_materialized"] is False
    assert result["selected_config_recoveries"][0]["does_not_upgrade_requirement_readiness"] is True
    assert (tmp_path / "original.bat").read_bytes() == original
    recovered["git_blob_sha"] = "0" * 40
    path.write_text(json.dumps(audit))
    with pytest.raises(ValueError, match="original and pinned bytes"):
        recovery_reference(task, {"files": [old]}, path)
