"""Optional agent hints cannot replace a frozen launcher or crash acceptance."""
from copy import deepcopy

import pytest

from scripts.run_portable_harness import recorder_hint_options


@pytest.mark.parametrize('launcher', ['./mvnw', '/workspace/project/mvnw'])
def test_wrapper_rejects_irrelevant_maven_hint_but_keeps_jdk(launcher):
    step = {'id': 'ci-step-1', 'runner': 'maven', 'argv': [launcher, '-B', 'clean', 'install']}
    original = deepcopy(step)
    options, audit = recorder_hint_options(step, {'ci-step-1': {
        'java_home': '/opt/java-21', 'maven_bin': '/opt/maven/bin/mvn'}})
    assert options == ['--java-home', '/opt/java-21']
    assert step == original
    assert audit['maven_bin']['status'] == 'ignored'
    assert audit['maven_bin']['reason'] == 'frozen_launcher_is_not_literal_mvn'
    assert audit['java_home']['status'] == 'applied'


def test_literal_maven_can_use_absolute_hint_without_changing_frozen_args():
    step = {'id': 'ci-step-1', 'runner': 'maven', 'argv': ['mvn', 'clean', 'verify']}
    original = deepcopy(step)
    options, audit = recorder_hint_options(step, {'ci-step-1': {'maven_bin': '/opt/maven/bin/mvn'}})
    assert options == ['--maven-bin', '/opt/maven/bin/mvn']
    assert audit['maven_bin']['status'] == 'applied'
    assert step == original


@pytest.mark.parametrize('value', ['', 17, [], {}, True, 'bin/mvn', '/opt/java\x00bad'])
def test_invalid_hint_is_disclosed_and_cannot_be_passed_to_subprocess(value):
    step = {'id': 's', 'runner': 'maven', 'argv': ['mvn', 'test']}
    options, audit = recorder_hint_options(step, {'s': {'java_home': value, 'maven_bin': value}})
    assert options == []
    assert all(row['status'] == 'ignored' for row in audit.values())


@pytest.mark.parametrize('hints', [None, [], 'invalid', {'s': None}, {'s': []}])
def test_malformed_hint_document_is_optional_and_audited(hints):
    options, audit = recorder_hint_options({'id': 's', 'runner': 'gradle', 'argv': ['./gradlew', 'test']}, hints)
    assert options == []
    assert audit['_shape']['status'] == 'ignored'


def test_gradle_never_uses_a_maven_override():
    options, audit = recorder_hint_options(
        {'id': 's', 'runner': 'gradle', 'argv': ['./gradlew', 'test']},
        {'s': {'maven_bin': '/opt/maven/bin/mvn'}},
    )
    assert options == []
    assert audit['maven_bin']['status'] == 'ignored'
