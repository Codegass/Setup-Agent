"""Experiment settings must reach the real Config loader, including advisor off."""

import os
from pathlib import Path

import pytest

from scripts.d3r2_campaign import config_environment
from scripts.run_portable_harness import effective_sag_config, prompt, sag_model_config


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
