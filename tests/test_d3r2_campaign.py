"""Campaign preparation and resume boundaries do not launch Docker or models."""

import json
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
