"""Rejected source commands explain exact params without rewriting reviewed intent."""

import json
import shlex
from pathlib import Path

import pytest

from sag.tools.build.backends import source_command_tokens

FIXTURE = Path(__file__).parent / "fixtures/d3r1_remediation/commons-cli-plan-params.json"
FLAGS = "--errors --show-version --batch-mode --no-transfer-progress"


def test_real_analyze_build_mismatch_names_exact_args_missing_flags_and_duplicate_goal():
    params = json.loads(FIXTURE.read_text())["build_params"]
    before = dict(params)
    with pytest.raises(ValueError) as caught:
        source_command_tokens(
            params["source_command"], params["system"], params["action"], params["args"]
        )
    message = str(caught.value)
    assert f"expected args='{FLAGS} clean'" in message
    assert (
        "missing tokens=['--errors', '--show-version', '--batch-mode', '--no-transfer-progress']"
        in message
    )
    assert "extra tokens=['verify']" in message
    assert "remove exactly one 'verify'" in message
    assert params == before


def test_real_analyze_test_mismatch_explains_verify_encoding_without_adding_test():
    params = json.loads(FIXTURE.read_text())["test_params"]
    with pytest.raises(ValueError) as caught:
        source_command_tokens(
            params["source_command"], params["system"], params["action"], params["args"]
        )
    message = str(caught.value)
    assert "does not execute Maven action=test" in message
    assert "if this reviewed source is intended" in message
    assert "action='verify'" in message
    assert f"args='{FLAGS} clean'" in message
    assert "not applied" in message


def test_maven_reordered_args_remain_invalid_even_with_equal_token_counts():
    with pytest.raises(ValueError) as caught:
        source_command_tokens("mvn -B clean verify -Pci", "maven", "verify", "clean -Pci -B")
    message = str(caught.value)
    assert "expected args='-B clean -Pci'" in message
    assert "missing tokens=[]; extra tokens=[]" in message
    assert "source order" in message


def test_maven_feedback_preserves_duplicate_goals_and_quoted_option_values():
    source = "mvn -Dlabel='two words' clean verify verify"
    expected = "'-Dlabel=two words' clean verify"
    with pytest.raises(ValueError) as caught:
        source_command_tokens(source, "maven", "verify", "clean")
    assert f"expected args={expected!r}" in str(caught.value)
    assert source_command_tokens(source, "maven", "verify", expected) == shlex.split(source)[1:]


def test_bare_maven_does_not_invent_default_goal():
    with pytest.raises(ValueError) as caught:
        source_command_tokens(f"mvn {FLAGS}", "maven", "verify", FLAGS)
    assert "defaultGoal" in str(caught.value)
    assert "every resolved goal" in str(caught.value)


def test_explicit_reviewed_full_default_goal_sequence_is_representable():
    # A literal reviewed POM defaultGoal is supplied by the caller, not resolved
    # or inserted by the facade. Quality goals must remain in the exact argv.
    goals = "clean verify apache-rat:check japicmp:cmp checkstyle:check spotbugs:check pmd:check javadoc:javadoc"
    source = f"mvn {FLAGS} {goals}"
    args = f"{FLAGS} {goals.replace('verify ', '', 1)}"
    assert source_command_tokens(source, "maven", "verify", args) == shlex.split(source)[1:]
    with pytest.raises(ValueError, match="apache-rat:check"):
        source_command_tokens(source, "maven", "verify", f"{FLAGS} clean")


@pytest.mark.parametrize("source", ["mvn test && echo ok", "mvn $GOALS", "make test"])
def test_feedback_never_accepts_shell_or_wrapper_recipes(source):
    with pytest.raises(ValueError):
        source_command_tokens(source, "maven", "test", "")
