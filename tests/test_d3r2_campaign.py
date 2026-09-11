"""Campaign preparation and resume boundaries do not launch Docker or models."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import d3r2_campaign as campaign


def target(path, repo="apache/commons-cli", sha=campaign.SMALL[0][2], matched=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"repo": repo, "sha": sha, "matched_cell": matched}))


def test_output_inside_sealed_archive_is_always_refused():
    with pytest.raises(ValueError, match="sealed"):
        campaign.safe_output(campaign.ROOT / "logs/d3r1-20260907/new-run")


def test_existing_output_is_refused_before_any_external_call(tmp_path, monkeypatch):
    monkeypatch.setattr(
        campaign, "command", lambda *_args, **_kw: pytest.fail("must not inspect or launch")
    )
    output = tmp_path / "existing-campaign"
    output.mkdir()
    with pytest.raises(FileExistsError):
        campaign.prepare(SimpleNamespace(out=output))


def test_fixed_cell_cannot_be_replaced_by_a_new_green_cell(tmp_path):
    projects = []
    for i in range(23):
        seat = f"p{i}"
        target(tmp_path / f"{seat}.json", repo=f"apache/{seat}", sha="a" * 40, matched="new-green")
        projects.append(
            dict(seat=seat, repo=f"apache/{seat}", sha="a" * 40, matched_cell="original")
        )
    with pytest.raises(ValueError, match="cell changed"):
        campaign.selected_projects("23", {"projects": projects}, tmp_path)


def test_small_pairs_have_identical_subjects_and_alternate_version_order(tmp_path):
    for seat, repo, sha in campaign.SMALL:
        target(tmp_path / f"{seat}.json", repo, sha)
    projects = campaign.selected_projects("paired", {}, tmp_path)
    assert [(p["seat"], p["variant"]) for p in projects] == [
        ("commons-cli", "baseline"),
        ("commons-cli", "candidate"),
        ("gson", "candidate"),
        ("gson", "baseline"),
    ]
    assert [(p["repo"], p["sha"]) for p in projects[:2]] == [campaign.SMALL[0][1:]] * 2


def test_baseline_never_receives_new_comparison_cli_flag():
    manifest = {"sources": {"candidate": {"path": "/candidate"}, "baseline": {"path": "/baseline"}}}
    project = dict(
        repo="apache/demo",
        sha="a" * 40,
        container="sag-new-project",
        target_file="/new-target.json",
        variant="candidate",
    )
    assert "--ci-target-file" in campaign.cli_command(manifest, project)
    assert "--ci-target-file" not in campaign.cli_command(
        manifest, project | {"variant": "baseline"}
    )


@pytest.mark.parametrize("change", ["bytes", "repo", "sha", "file_removed", "baseline"])
def test_required_task_drift_is_rejected_before_container_inspection(tmp_path, monkeypatch, change):
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "repo": "apache/demo",
                "sha": "a" * 40,
                "steps": [{"id": "test", "runner": "maven", "argv": ["mvn", "test"]}],
            }
        )
    )
    project = dict(
        repo="apache/demo",
        sha="a" * 40,
        variant="candidate",
        run_key="demo",
        acceptance_task_file=str(task_path),
        acceptance_task_sha256=campaign.digest(task_path),
    )
    if change == "bytes":
        task_path.write_text(task_path.read_text() + "\n")
    elif change in {"repo", "sha"}:
        project[change] = "apache/another" if change == "repo" else "b" * 40
    elif change == "file_removed":
        project.pop("acceptance_task_file")
    else:
        project["variant"] = "baseline"
    monkeypatch.setattr(campaign, "inspect_container", lambda *_a: pytest.fail("no Docker call"))
    with pytest.raises((ValueError, RuntimeError), match="[Tt]ask|baseline"):
        campaign.run_one(tmp_path, {}, project)


def test_resuming_without_the_required_task_cannot_reuse_its_result(tmp_path):
    directory = tmp_path / "runs/demo"
    directory.mkdir(parents=True)
    manifest = {"sources": {"candidate": {"sha": "c" * 40}}}
    project = dict(
        run_key="demo", repo="apache/demo", sha="a" * 40, variant="candidate", target_sha256="t"
    )
    (directory / "result.json").write_text(
        json.dumps(
            {
                **project,
                "target_sha": project["sha"],
                "sag_sha": "c" * 40,
                "target_record_file_sha256": "t",
                "acceptance_task_file_sha256": "f" * 64,
            }
        )
    )
    with pytest.raises(RuntimeError, match="Existing result"):
        campaign.run_one(tmp_path, manifest, project)


def test_candidate_cli_keeps_task_and_ci_inputs_separate():
    manifest = {"sources": {"candidate": {"path": "/candidate"}, "baseline": {"path": "/baseline"}}}
    project = dict(
        repo="apache/demo",
        sha="a" * 40,
        container="sag-demo",
        variant="candidate",
        target_file="/ci.json",
        acceptance_command="mvn test",
        acceptance_task_file="/task.json",
    )
    argv = campaign.cli_command(manifest, project)
    for flag, value in [
        ("--ci-target-file", "/ci.json"),
        ("--acceptance-command", "mvn test"),
        ("--acceptance-task-file", "/task.json"),
    ]:
        assert argv[argv.index(flag) + 1] == value
    assert "--acceptance-task-file" not in campaign.cli_command(
        manifest, project | {"variant": "baseline"}
    )


@pytest.mark.parametrize("task_pin_status", ["valid", "missing", "different"])
def test_attempt_archives_task_bytes_and_checks_the_actual_run_pin(
    tmp_path, monkeypatch, task_pin_status
):
    from pathlib import Path
    from sag.agent.acceptance_task import load_acceptance_task

    task_path = tmp_path / "task.json"
    raw = json.dumps(
        {
            "repo": "apache/demo",
            "sha": "a" * 40,
            "steps": [{"id": "test", "runner": "maven", "argv": ["mvn", "test"]}],
        }
    )
    task_path.write_text(raw)
    task = load_acceptance_task(task_path)
    target_file = tmp_path / "ci.json"
    target_file.write_text("{}")
    manifest = {
        "sources": {"candidate": {"path": str(tmp_path), "sha": "b" * 40, "files": {}}},
        "config": {"max_wall_clock_seconds": 1},
        "docker_image_id": "sha256:" + "f" * 64,
    }
    project = dict(
        run_key="demo",
        seat="demo",
        variant="candidate",
        repo=task.repo,
        sha=task.sha,
        target_file=str(target_file),
        target_sha256=campaign.digest(target_file),
        container="sag-demo",
        acceptance_task_file=str(task_path),
        acceptance_task_sha256=campaign.digest(task_path),
    )
    task_pin = {"sha256": task.sha256, "definition": task.model_dump(mode="json")}
    if task_pin_status == "different":
        task_pin["sha256"] = "c" * 64
    config = {} if task_pin_status == "missing" else {"acceptance_task": task_pin}
    pin = {
        "target_repo_sha": task.sha,
        "sag_git_sha": "b" * 40,
        "container_image_digest": manifest["docker_image_id"],
        "sanitized_config": config,
    }
    payload = {"run_id": "task-run", "metrics": {"verdict": "success"}, "pin": pin}

    class FinishedProcess:
        returncode, pid = 0, 123456789

        def __init__(self, argv, **kwargs):
            copied = Path(argv[argv.index("--acceptance-task-file") + 1])
            assert copied == kwargs["cwd"] / "acceptance-task.json"
            # Changing the caller's original file after dispatch cannot alter
            # the bytes the child receives or its archived provenance.
            task_path.write_text("changed after dispatch")
            assert copied.read_text() == raw
            (kwargs["cwd"] / "logs/session_fake").mkdir(parents=True)

        def poll(self):
            return self.returncode

    for name in ["verify_source", "runtime_environment", "effective_config"]:
        monkeypatch.setattr(campaign, name, lambda *_a: {})
    monkeypatch.setattr(campaign, "inspect_container", lambda *_a: None)
    monkeypatch.setattr(campaign.subprocess, "Popen", FinishedProcess)
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    result = campaign.run_one(tmp_path, manifest, project)
    assert result["authority_ok"] is (task_pin_status == "valid")
    assert result["acceptance_task_file_sha256"] == project["acceptance_task_sha256"]
    if task_pin_status != "valid":
        assert "acceptance_task" in result["runner_error"]
        assert "verdict" not in result


def test_container_name_alone_is_not_ownership():
    project = {"container": "sag-new-project"}
    started = "2026-09-09T01:00:00+00:00"
    data = {
        "Image": "sha256:image",
        "Created": "2026-09-09T01:00:01Z",
        "Config": {"Labels": {"setup-agent.project": "new-project"}},
    }
    assert campaign.owned_container(data, project, started, "sha256:image")
    assert not campaign.owned_container(
        data | {"Created": "2026-09-08T01:00:00Z"}, project, started, "sha256:image"
    )
    assert not campaign.owned_container(
        data | {"Image": "foreign"}, project, started, "sha256:image"
    )
    assert not campaign.owned_container(
        data | {"Config": {"Labels": {}}}, project, started, "sha256:image"
    )


def test_docker_daemon_error_is_not_treated_as_an_available_name(monkeypatch):
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stderr="Cannot connect to Docker daemon"),
    )
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        campaign.inspect_container("sag-new")


@pytest.mark.parametrize(
    "detail",
    [
        "Error: No such object: sag-new",
        "error: no such object: sag-new",
        "No such container: sag-new",
    ],
)
def test_missing_container_inventory_accepts_daemon_message_case(monkeypatch, detail):
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stderr=detail),
    )
    assert campaign.inspect_container("sag-new") is None


def test_completed_attempt_is_returned_without_rerun(tmp_path, monkeypatch):
    project = dict(
        run_key="p-candidate",
        variant="candidate",
        repo="apache/p",
        sha="a" * 40,
        target_sha256="target-hash",
    )
    manifest = {"sources": {"candidate": {"sha": "b" * 40}}}
    result = dict(
        run_key="p-candidate",
        variant="candidate",
        repo="apache/p",
        target_sha="a" * 40,
        sag_sha="b" * 40,
        target_record_file_sha256="target-hash",
        exit_code=1,
        outer_timeout=True,
    )
    campaign.save(tmp_path / "runs/p-candidate/result.json", result)
    monkeypatch.setattr(campaign, "inspect_container", lambda *_a: pytest.fail("must not rerun"))
    assert campaign.run_one(tmp_path, manifest, project) == result
    campaign.save(tmp_path / "runs/p-candidate/result.json", result | {"target_sha": "c" * 40})
    with pytest.raises(RuntimeError, match="does not belong"):
        campaign.run_one(tmp_path, manifest, project)


def test_incomplete_attempt_is_never_overwritten(tmp_path):
    (tmp_path / "runs/p").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Incomplete prior attempt"):
        campaign.run_one(tmp_path, {}, {"run_key": "p"})


def test_config_freezes_budgets_models_and_nondefault_token_keys():
    config = {
        "thinking_model": "gpt-5.4-mini",
        "action_model": "gpt-5.4-mini",
        "max_iterations": 75,
        "max_wall_clock_seconds": 1200,
        "thinking_max_tokens": 16000,
        "action_max_tokens": 10000,
        "phase_min_floors": {"build": 10},
    }
    env = campaign.config_environment(config)
    assert env["SAG_MAX_ITERATIONS"] == "75"
    assert env["SAG_MAX_WALL_CLOCK_SECONDS"] == "1200"
    assert env["SAG_MAX_THINKING_TOKENS"] == "16000"
    assert env["SAG_MAX_ACTION_TOKENS"] == "10000"
    assert "SAG_PHASE_MIN_FLOORS" not in env  # Actual Config defaults are checked before launch.


@pytest.mark.parametrize(
    "changed", [None, "target_repo_sha", "sag_git_sha", "container_image_digest", "missing_pin"]
)
def test_collector_authority_cannot_substitute_a_different_prepared_subject(
    tmp_path, monkeypatch, changed
):
    target_file = tmp_path / "target.json"
    target_file.write_text("{}")
    manifest = {
        "sources": {"candidate": {"path": str(tmp_path), "sha": "b" * 40, "files": {}}},
        "config": {"max_wall_clock_seconds": 1},
        "docker_image_id": "sha256:" + "f" * 64,
    }
    project = {
        "run_key": "subject-candidate",
        "seat": "subject",
        "variant": "candidate",
        "repo": "apache/subject",
        "sha": "a" * 40,
        "target_sha256": campaign.digest(target_file),
        "target_file": str(target_file),
        "container": "sag-candidate-subject",
    }
    pin = {
        "target_repo_sha": project["sha"],
        "sag_git_sha": manifest["sources"]["candidate"]["sha"],
        "container_image_digest": manifest["docker_image_id"],
    }
    if changed and changed != "missing_pin":
        pin[changed] = "c" * 40 if changed.endswith("sha") else "sha256:" + "d" * 64
    payload = {"run_id": "authorized-run", "metrics": {"verdict": "success"}, "pin": pin}
    if changed == "missing_pin":
        del payload["pin"]

    class FinishedProcess:
        returncode = 0
        pid = 123456789

        def __init__(self, *_args, **kwargs):
            (kwargs["cwd"] / "logs/session_fake").mkdir(parents=True)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(campaign, "inspect_container", lambda *_a: None)
    monkeypatch.setattr(campaign, "verify_source", lambda *_a: {})
    monkeypatch.setattr(campaign, "runtime_environment", lambda *_a: {})
    monkeypatch.setattr(campaign, "effective_config", lambda *_a: {})
    monkeypatch.setattr(campaign.subprocess, "Popen", FinishedProcess)
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    result = campaign.run_one(tmp_path, manifest, project)
    assert result["authority_ok"] is (changed is None)
    if changed is None:
        assert result["verdict"] == "success"
    else:
        assert "Collected run pin differs from prepared values" in result["runner_error"]
        assert "verdict" not in result
    # Keep the authorized-but-wrong observation available for attribution.
    assert json.loads((tmp_path / "runs/subject-candidate/collected.json").read_text()) == payload


def _archive_with_patch(tmp_path, monkeypatch, produce_patch):
    calls = []
    monkeypatch.setattr(
        campaign,
        "inspect_container",
        lambda _identity: {"Id": "owned", "State": {"Running": True}, "Mounts": []},
    )

    def run(argv, **kwargs):
        calls.append(argv)
        if "--binary" in argv:
            assert argv == [
                "docker",
                "exec",
                "owned",
                "git",
                "-C",
                "/workspace/demo",
                "diff",
                "HEAD",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
            ]
            assert kwargs["stdout"].name.endswith("source.patch")
            assert not kwargs.get("capture_output") and not kwargs.get("text")
            return produce_patch(argv, kwargs)
        text = kwargs.get("text")
        output = " M tracked.bin\n?? generated/\n" if "status" in argv else ""
        return SimpleNamespace(
            returncode=0, stdout=output if text else b"", stderr="" if text else b""
        )

    monkeypatch.setattr(campaign.subprocess, "run", run)
    directory = tmp_path / "archive"
    directory.mkdir()
    result = campaign.archive_container(
        {"repo": "apache/demo", "seat": "alias"}, directory, "owned"
    )
    assert calls[-1] == ["docker", "stop", "--time", "20", "owned"]
    assert result["raw_reports"]["exit_code"] == 0  # Patch failure must not skip report recovery.
    assert result["source_status"]["output"].endswith("?? generated/\n")
    assert (
        json.loads((directory / "diagnostics.json").read_text())["source_patch"]
        == result["source_patch"]
    )
    return directory / "source.patch", result["source_patch"]


@pytest.mark.parametrize(
    "body,exit_code,timed_out,expected_complete,expected_truncated",
    [
        (b"", 0, False, True, False),
        (b"binary\x00\xff\xfe\n", 0, False, True, False),
        (b"partial diff", 128, False, False, False),
        (b"x" * 32, 0, False, True, False),
        (b"x" * 33, 0, False, False, True),
        (b"unfinished diff", None, True, False, False),
    ],
    ids=["empty", "binary", "failed", "at-limit", "truncated", "timeout"],
)
def test_source_patch_archive_never_promotes_incomplete_bytes(
    tmp_path, monkeypatch, body, exit_code, timed_out, expected_complete, expected_truncated
):
    monkeypatch.setattr(campaign, "MAX_SOURCE_PATCH_BYTES", 32)

    def produce(argv, kwargs):
        kwargs["stdout"].write(body)
        if timed_out:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=exit_code, stderr=b"git failure" if exit_code else b"")

    path, facts = _archive_with_patch(tmp_path, monkeypatch, produce)
    retained = body[:32]
    assert path.read_bytes() == retained
    assert facts["sha256"] == hashlib.sha256(retained).hexdigest()
    assert facts["collected_bytes"] == len(body)
    assert facts["retained_bytes"] == len(retained)
    assert facts["complete"] is expected_complete
    assert facts["truncated"] is expected_truncated
    assert facts["exit_code"] == exit_code
    if timed_out:
        assert facts["timeout"] is True


def test_source_patch_preserves_real_staged_binary_and_unstaged_changes(tmp_path, monkeypatch):
    git = subprocess.run
    repo = tmp_path / "repo"
    repo.mkdir()

    def command(*args):
        return git(["git", "-C", str(repo), *args], check=True, capture_output=True)

    command("init", "-q")
    (repo / "tracked.bin").write_bytes(b"original\x00binary")
    (repo / "pom.xml").write_text("<project>original</project>\n")
    command("add", ".")
    command(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    (repo / "tracked.bin").write_bytes(b"replacement\x00binary")
    command("add", "tracked.bin")
    (repo / "pom.xml").write_text("<project>changed</project>\n")
    expected = command("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv").stdout
    assert b"GIT binary patch" in expected and b"+<project>changed</project>" in expected

    def produce(_argv, kwargs):
        # Only Docker transport is replaced; actual git emits the archived bytes.
        return git(
            ["git", "-C", str(repo), "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"],
            **kwargs,
        )

    path, facts = _archive_with_patch(tmp_path, monkeypatch, produce)
    assert path.read_bytes() == expected
    assert facts["complete"] is True and facts["truncated"] is False
    assert facts["sha256"] == hashlib.sha256(expected).hexdigest()


def _job_metadata(directory, entries, *, mirror=False):
    root = (
        directory / "container-evidence/.setup_agent"
        if mirror
        else directory / "logs/session_fixture/.setup_agent"
    )
    path = root / "contexts/output_index.json"
    campaign.save(path, {ref: {"metadata": value} for ref, value in entries.items()})
    return path


def _job_claim(body, path="/tmp/sag_jobs/e2f08890b266.log"):
    return {
        "full_log_path": path,
        "full_log_bytes": len(body),
        "full_log_sha256": hashlib.sha256(body).hexdigest(),
        "output_storage_truncated": True,
    }


def _job_transport(monkeypatch, source):
    real_run = subprocess.run
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["docker", "cp"]
        assert argv[2].startswith("owned:/tmp/sag_jobs/")
        return real_run(["/bin/cp", str(source), argv[3]], **kwargs)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "body",
    [b"", b"head\n" + b"x" * 1247893 + b"\nTAIL-MUST-SURVIVE\n"],
    ids=["empty", "long-with-footer"],
)
def test_raw_job_archive_copies_complete_file_instead_of_output_storage_summary(
    tmp_path, monkeypatch, body
):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_ref": _job_claim(body)})
    physical = tmp_path / "physical.log"
    physical.write_bytes(body)
    calls = _job_transport(monkeypatch, physical)
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is True, facts
    assert len(calls) == 1
    record = facts["files"][0]
    assert (directory / record["path"]).read_bytes() == body
    assert record["bytes"] == len(body)
    assert record["sha256"] == hashlib.sha256(body).hexdigest()
    assert record["declared_match"] == "exact"
    assert record["receipt_authority"] == "not_evaluated_by_archiver"


def test_raw_job_archive_preserves_conflicting_claims_for_the_same_path(tmp_path, monkeypatch):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_a": _job_claim(b"earlier")})
    _job_metadata(directory, {"output_b": _job_claim(b"later")}, mirror=True)
    physical = tmp_path / "physical.log"
    physical.write_bytes(b"later")
    calls = _job_transport(monkeypatch, physical)
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    record = facts["files"][0]
    assert len(calls) == 1 and len(record["metadata_claims"]) == 2
    assert {c["declared_sha256"] for c in record["metadata_claims"]} == {
        hashlib.sha256(b"earlier").hexdigest(),
        hashlib.sha256(b"later").hexdigest(),
    }
    assert record["declared_match"] == "conflicting_metadata"
    assert facts["complete"] is False
    assert (directory / record["path"]).read_bytes() == b"later"


@pytest.mark.parametrize("failure", ["missing", "timeout", "mismatch", "symlink"])
def test_raw_job_archive_never_promotes_unavailable_or_unbound_bytes(
    tmp_path, monkeypatch, failure
):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_ref": _job_claim(b"expected")})

    def run(argv, **kwargs):
        if failure == "missing":
            return SimpleNamespace(returncode=1, stderr="no such file")
        if failure == "timeout":
            campaign.Path(argv[3]).write_bytes(b"partial copy")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        path = campaign.Path(argv[3])
        if failure == "symlink":
            path.symlink_to(tmp_path / "unrelated")
        else:
            path.write_bytes(b"other")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(campaign.subprocess, "run", run)
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is False and facts["files"][0]["complete"] is False
    if failure == "timeout":
        assert facts["files"][0]["bytes"] == len(b"partial copy")
        assert facts["files"][0]["sha256"] == hashlib.sha256(b"partial copy").hexdigest()


@pytest.mark.parametrize(
    "metadata",
    [
        {"output_storage_truncated": True},
        _job_claim(b"", "/tmp/sag_jobs/../elsewhere.log"),
        _job_claim(b"", "/workspace/arbitrary.log"),
    ],
)
def test_raw_job_archive_missing_or_out_of_bounds_path_is_not_not_required(
    tmp_path, monkeypatch, metadata
):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_ref": metadata})
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *_a, **_k: pytest.fail("must not copy an unscoped path")
    )
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is False and facts["status"] == "incomplete"
    assert facts["errors"]


def test_raw_job_archive_untruncated_output_needs_no_job_file(tmp_path, monkeypatch):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_ref": {"output_storage_truncated": False}})
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *_a, **_k: pytest.fail("no referenced raw job file")
    )
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is True and facts["status"] == "not_required"


def test_raw_job_archive_reads_journal_when_index_is_absent(tmp_path, monkeypatch):
    directory = tmp_path / "attempt"
    journal = directory / "logs/session_fixture/.setup_agent/contexts/full_outputs.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"ref_id": "output_ref", "metadata": _job_claim(b"all")}) + "\n")
    physical = tmp_path / "physical.log"
    physical.write_bytes(b"all")
    _job_transport(monkeypatch, physical)
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert (
        facts["complete"] is True
        and facts["files"][0]["metadata_claims"][0]["output_ref"] == "output_ref"
    )


def test_raw_job_archive_corrupt_metadata_is_unavailable(tmp_path, monkeypatch):
    directory = tmp_path / "attempt"
    path = _job_metadata(directory, {})
    path.write_text("{")
    monkeypatch.setattr(campaign.subprocess, "run", lambda *_a, **_k: pytest.fail("no known path"))
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is False and facts["errors"]


def test_raw_job_collection_exception_still_stops_owned_container(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        campaign,
        "inspect_container",
        lambda *_a: {"Id": "owned", "State": {"Running": True}, "Mounts": []},
    )

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fail(*_a):
        raise OSError("raw archive unavailable")

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "archive_raw_job_logs", fail)
    directory = tmp_path / "attempt"
    directory.mkdir()
    with pytest.raises(OSError, match="raw archive"):
        campaign.archive_container({"repo": "apache/demo"}, directory, "owned")
    assert calls[-1] == ["docker", "stop", "--time", "20", "owned"]


@pytest.mark.parametrize(
    "physical,declared,expected",
    [
        (b"runner output\n", b"runner output", True),
        (b"\n", b"", True),
        (b"runner output\n\n", b"runner output", False),
        (b"runner output ", b"runner output", False),
        (b"wrong content\n", b"right content", False),
    ],
    ids=["one-lf", "empty-text-one-lf", "two-lfs", "space", "different-content"],
)
def test_raw_job_transport_one_lf_variant_is_explicit_and_never_changes_physical_bytes(
    tmp_path, monkeypatch, physical, declared, expected
):
    directory = tmp_path / "attempt"
    _job_metadata(directory, {"output_ref": _job_claim(declared)})
    original = tmp_path / "physical.log"
    original.write_bytes(physical)
    _job_transport(monkeypatch, original)
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    row = facts["files"][0]
    assert facts["complete"] is expected
    assert row["physical_copy_complete"] is True
    assert (directory / row["path"]).read_bytes() == physical
    assert row["bytes"] == len(physical)
    assert row["sha256"] == hashlib.sha256(physical).hexdigest()
    if expected:
        assert row["declared_match"] == "normalized_output_match"
        assert row["transform"] == "remove_one_terminal_lf"
        assert row["normalized_bytes"] == len(declared)
        assert row["normalized_sha256"] == hashlib.sha256(declared).hexdigest()
        assert row["sha256"] != row["normalized_sha256"]
    else:
        assert row["declared_match"] == "mismatch"
        assert "transform" not in row


@pytest.mark.parametrize("raw_summary", [None, {"complete": False}, {"complete": True}])
def test_run_archive_completeness_requires_raw_job_log_collection(
    tmp_path, monkeypatch, raw_summary
):
    target_file = tmp_path / "target.json"
    target_file.write_text("{}")
    manifest = {
        "sources": {"candidate": {"path": str(tmp_path), "sha": "b" * 40, "files": {}}},
        "config": {"max_wall_clock_seconds": 1},
        "docker_image_id": "sha256:" + "f" * 64,
    }
    project = {
        "run_key": "subject-candidate",
        "seat": "subject",
        "variant": "candidate",
        "repo": "apache/subject",
        "sha": "a" * 40,
        "target_sha256": campaign.digest(target_file),
        "target_file": str(target_file),
        "container": "sag-candidate-subject",
    }
    payload = {
        "run_id": "authorized-run",
        "metrics": {"verdict": "success"},
        "pin": {
            "target_repo_sha": project["sha"],
            "sag_git_sha": "b" * 40,
            "container_image_digest": manifest["docker_image_id"],
        },
    }
    owned = {
        "Id": "owned",
        "Image": manifest["docker_image_id"],
        "Created": "2099-01-01T00:00:00Z",
        "Config": {"Labels": {"setup-agent.project": "candidate-subject"}},
    }
    inventory = iter([None, owned])

    class FinishedProcess:
        returncode = 0
        pid = 123456789

        def __init__(self, *_args, **kwargs):
            (kwargs["cwd"] / "logs/session_fake").mkdir(parents=True)

        def poll(self):
            return self.returncode

    diagnostics = {
        "evidence_copy": {"exit_code": 0},
        "raw_reports": {"exit_code": 0},
        "report_discovery": {"exit_code": 0},
        "stop": {"exit_code": 0},
    }
    if raw_summary is not None:
        diagnostics["raw_job_logs"] = raw_summary
    monkeypatch.setattr(campaign, "inspect_container", lambda *_a: next(inventory))
    monkeypatch.setattr(campaign, "archive_container", lambda *_a: diagnostics)
    monkeypatch.setattr(campaign, "verify_source", lambda *_a: {})
    monkeypatch.setattr(campaign, "runtime_environment", lambda *_a: {})
    monkeypatch.setattr(campaign, "effective_config", lambda *_a: {})
    monkeypatch.setattr(campaign.subprocess, "Popen", FinishedProcess)
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    result = campaign.run_one(tmp_path, manifest, project)
    assert result["evidence_archive_complete"] is (raw_summary == {"complete": True})
    # Archive completeness never changes the collected local verdict or grants
    # a receipt authority of its own.
    assert result["verdict"] == "success" and result["authority_ok"] is True


def test_raw_job_metadata_symlink_cannot_read_outside_the_archive(tmp_path, monkeypatch):
    directory = tmp_path / "attempt"
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"output_ref": {"metadata": _job_claim(b"outside")}}))
    path = directory / "logs/session_fixture/.setup_agent/contexts/output_index.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *_a, **_k: pytest.fail("unscoped metadata")
    )
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["complete"] is False and facts["files"] == [] and facts["errors"]


@pytest.mark.parametrize("kind", ["dangling-symlink", "directory"])
def test_raw_job_metadata_damaged_entry_cannot_be_treated_as_absent(tmp_path, monkeypatch, kind):
    directory = tmp_path / "attempt"
    path = directory / "logs/session_fixture/.setup_agent/contexts/output_index.json"
    path.parent.mkdir(parents=True)
    if kind == "dangling-symlink":
        path.symlink_to(tmp_path / "missing.json")
    else:
        path.mkdir()
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *_a, **_k: pytest.fail("damaged metadata")
    )
    facts = campaign.archive_raw_job_logs(directory, directory / "container-evidence", "owned")
    assert facts["status"] == "incomplete" and facts["complete"] is False
    assert facts["errors"] and facts["files"] == []
