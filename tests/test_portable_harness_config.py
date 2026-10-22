"""Experiment settings must reach the real Config loader, including advisor off."""

import os
from pathlib import Path

import pytest

from scripts.d3r2_campaign import config_environment
from scripts.run_portable_harness import (
    baseline_model_environment,
    checkout_pinned_commit,
    claude_arm_environment,
    effective_sag_config,
    launch_environment,
    prompt,
    sag_model_config,
)


def environment(config):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAG_")}
    env.update(config_environment(config))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


@pytest.mark.parametrize(
    "setting,value",
    [
        ("advisor_context_selection", "relevant"),
        ("advisor_trigger_policy", "problems"),
        ("compact_setup_prompt", True),
        ("reuse_verified_test_phase", True),
        ("provision_from_task", False),
        ("export_runtime_handoff", True),
        ("advisor_mode", "off"),
    ],
)
def test_policy_survives_runner_environment_and_real_child_loader(tmp_path, setting, value):
    protocol = {"model_config": {setting: value}}
    config = sag_model_config(protocol, image="frozen-image", seconds=7200)
    actual = effective_sag_config(config, environment(config), tmp_path)
    assert actual[setting] == value
    assert protocol == {"model_config": {setting: value}}


@pytest.mark.parametrize(
    "setting,value",
    [
        ("advisor_context_selection", "relevant"),
        ("advisor_trigger_policy", "problems"),
        ("compact_setup_prompt", True),
        ("reuse_verified_test_phase", True),
        ("provision_from_task", False),
        ("export_runtime_handoff", True),
        ("advisor_mode", "off"),
    ],
)
def test_top_level_policy_cannot_silently_be_ignored(setting, value):
    with pytest.raises(ValueError, match="inside model_config"):
        sag_model_config({"model_config": {}, setting: value}, image="image", seconds=7200)


def test_missing_environment_mapping_is_detected_before_inference(tmp_path):
    config = sag_model_config(
        {"model_config": {"advisor_context_selection": "relevant"}}, image="image", seconds=7200
    )
    env = environment(config)
    del env["SAG_ADVISOR_CONTEXT_SELECTION"]
    with pytest.raises(ValueError, match="advisor_context_selection"):
        effective_sag_config(config, env, tmp_path)


def test_unknown_policy_is_not_accepted_as_an_ablation():
    with pytest.raises(ValueError, match="Unknown model settings"):
        sag_model_config(
            {"model_config": {"advisor_triger_policy": "problems"}}, image="image", seconds=7200
        )


def test_explicit_off_and_cli_off_are_both_preserved():
    assert (
        sag_model_config({"model_config": {"advisor_mode": "off"}}, image="image", seconds=7200)[
            "advisor_mode"
        ]
        == "off"
    )
    assert (
        sag_model_config(
            {"model_config": {"advisor_mode": "openai/gpt-5.4-mini"}},
            image="image",
            seconds=7200,
            no_advisor=True,
        )["advisor_mode"]
        == "off"
    )


def test_common_brief_preserves_per_step_reviewed_environment():
    task = {'repo': 'example/project', 'sha': 'a'*40, 'steps': [
        {'id': 'one', 'cwd': '.', 'argv': ['mvn', 'test']},
        {'id': 'two', 'cwd': '.', 'argv': ['mvn', 'verify']} ]}
    spec = {'steps': [{'step_id': 'one', 'environment': {
        'MAVEN_OPTS': '-Xmx2g -Dexample.required=true'}}, {'step_id': 'two'}]}
    brief = prompt(task, '/work/project', 7200, spec)
    assert brief.count('"MAVEN_OPTS": "-Xmx2g -Dexample.required=true"') == 1
    assert '"id": "two"' in brief
    automatic = prompt(task, '/work/project', 7200, spec, runtime_handoff=True)
    assert '"MAVEN_OPTS": "-Xmx2g -Dexample.required=true"' in automatic
    assert brief == automatic
    assert 'toolpaths.json' not in brief
    assert 'will rerun' not in brief
    assert 'will not run missing commands' in brief
    assert 'wait for every required command' in brief


def _fake_dex(fail_first_checkout, head):
    calls = []

    def dex(argv, **kwargs):
        calls.append(argv)
        if argv[:4] == ['git', '-C', '/workspace/project', 'checkout'] and fail_first_checkout and len(
            [c for c in calls if c[:4] == argv[:4]]
        ) == 1:
            raise RuntimeError("fatal: reference is not a tree: " + argv[-1])
        if argv[-2:] == ['rev-parse', 'HEAD']:
            return head
        return ''

    return dex, calls


def test_pinned_commit_no_branch_reaches_is_fetched_directly():
    sha = 'db8698e9ea679b8319b9e07716c664788e856b2b'
    dex, calls = _fake_dex(fail_first_checkout=True, head=sha)
    checkout_pinned_commit(dex, '/workspace/project', sha)
    assert [c[3:] for c in calls] == [
        ['checkout', '--detach', sha],
        ['fetch', '--force', 'origin', sha],
        ['checkout', '--detach', sha],
        ['rev-parse', 'HEAD'],
    ]


def test_reachable_pinned_commit_is_checked_out_without_a_fetch():
    sha = 'db8698e9ea679b8319b9e07716c664788e856b2b'
    dex, calls = _fake_dex(fail_first_checkout=False, head=sha)
    checkout_pinned_commit(dex, '/workspace/project', sha)
    assert [c[3:] for c in calls] == [['checkout', '--detach', sha], ['rev-parse', 'HEAD']]


def test_checkout_at_another_commit_is_refused():
    dex, _ = _fake_dex(fail_first_checkout=False, head='0' * 40)
    with pytest.raises(ValueError):
        checkout_pinned_commit(dex, '/workspace/project', 'db8698e9ea679b8319b9e07716c664788e856b2b')


def _baseline_environment(arm, seconds=7200):
    return baseline_model_environment(
        arm, token="secret-token", endpoint="http://host.docker.internal:18571", seconds=seconds
    )


def test_claude_shell_may_wait_for_one_command_as_long_as_the_attempt_budget():
    """Claude Code's Bash tool moved pinned builds to the background at its 600 s default
    and the session ended before they finished (activemq, cayenne, httpcomponents-client,
    iotdb); its documented limits are raised to the attempt budget, in milliseconds."""
    assert claude_arm_environment(7200) == {
        "BASH_DEFAULT_TIMEOUT_MS": "7200000",
        "BASH_MAX_TIMEOUT_MS": "7200000",
    }
    env = _baseline_environment("claude", seconds=5400)
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == env["BASH_MAX_TIMEOUT_MS"] == "5400000"
    listed = launch_environment(env)
    assert listed["BASH_DEFAULT_TIMEOUT_MS"] == listed["BASH_MAX_TIMEOUT_MS"] == "5400000"


def test_opencode_shell_limits_are_left_to_the_client():
    env = _baseline_environment("opencode")
    assert "BASH_DEFAULT_TIMEOUT_MS" not in env and "BASH_MAX_TIMEOUT_MS" not in env


def test_launch_listing_keeps_redacting_gateway_credentials():
    listed = launch_environment(_baseline_environment("claude"))
    assert "secret-token" not in listed.values()
    assert listed["ANTHROPIC_API_KEY"] == "<per-attempt gateway credential>"
    assert listed["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:18571"


@pytest.mark.parametrize("arm", ["claude", "opencode"])
def test_baseline_shells_run_with_a_utf8_locale(arm):
    """Claude Code's shell reported platform encoding ANSI_X3.4-1968, which failed
    charset-sensitive tests in commons-codec and commons-csv; CI runners are UTF-8,
    and SAG's dispatch prefix already exports C.UTF-8."""
    env = _baseline_environment(arm)
    assert env["LANG"] == env["LC_ALL"] == "C.UTF-8"
    listed = launch_environment(env)
    assert listed["LANG"] == listed["LC_ALL"] == "C.UTF-8"
