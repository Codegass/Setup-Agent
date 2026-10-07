"""Resource policy reaches container creation without contacting Docker."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sag.config.settings import Config
from sag.docker_orch.orch import DockerOrchestrator


def make_container(monkeypatch, config):
    observed = {}
    container = SimpleNamespace(start=lambda: observed.update(started=True))

    def create(**kwargs):
        observed.update(kwargs)
        return container

    orchestrator = object.__new__(DockerOrchestrator)
    orchestrator.config = config
    orchestrator.project_name = "resource-test"
    orchestrator.container_name = "sag-resource-test"
    orchestrator.base_image = "sha256:frozen-test-image"
    orchestrator.client = SimpleNamespace(containers=SimpleNamespace(create=create))
    monkeypatch.setattr(orchestrator, "container_exists", lambda: False)
    monkeypatch.setattr(orchestrator, "_ensure_image_available", lambda: True)
    monkeypatch.setattr(orchestrator, "_wait_for_container_ready", lambda: True)
    monkeypatch.setattr(orchestrator, "_setup_container_environment", lambda: None)
    assert orchestrator.create_and_start_container()
    assert observed["started"] is True
    return observed


def test_opt_in_limits_reach_actual_container_creation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAG_DOCKER_MEMORY_LIMIT_BYTES", str(8 * 1024**3))
    monkeypatch.setenv("SAG_DOCKER_CPU_LIMIT", "4")
    config = Config.from_env()
    observed = make_container(monkeypatch, config)
    assert observed["mem_limit"] == 8 * 1024**3
    assert observed["memswap_limit"] == 8 * 1024**3
    assert observed["nano_cpus"] == 4_000_000_000


def test_absent_limits_preserve_unrestricted_docker_options(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SAG_DOCKER_MEMORY_LIMIT_BYTES", raising=False)
    monkeypatch.delenv("SAG_DOCKER_CPU_LIMIT", raising=False)
    observed = make_container(monkeypatch, Config.from_env())
    assert "mem_limit" not in observed
    assert "memswap_limit" not in observed
    assert "nano_cpus" not in observed


def test_fractional_cpu_quota_and_independent_memory_default(monkeypatch):
    observed = make_container(monkeypatch, Config(docker_cpu_limit=0.5))
    assert observed["nano_cpus"] == 500_000_000
    assert "mem_limit" not in observed
    assert "memswap_limit" not in observed


@pytest.mark.parametrize("field,value", [
    ("docker_memory_limit_bytes", 0),
    ("docker_memory_limit_bytes", -1),
    ("docker_memory_limit_bytes", True),
    ("docker_memory_limit_bytes", 0.5),
    ("docker_cpu_limit", 0),
    ("docker_cpu_limit", -1),
    ("docker_cpu_limit", True),
    ("docker_cpu_limit", float("nan")),
    ("docker_cpu_limit", float("inf")),
    ("docker_cpu_limit", 1e-10),
])
def test_invalid_limits_rejected_before_container_start(field, value):
    with pytest.raises(ValidationError):
        Config(**{field: value})


@pytest.mark.parametrize("name,value", [
    ("SAG_DOCKER_MEMORY_LIMIT_BYTES", "0"),
    ("SAG_DOCKER_MEMORY_LIMIT_BYTES", "8g"),
    ("SAG_DOCKER_CPU_LIMIT", "-4"),
    ("SAG_DOCKER_CPU_LIMIT", "nan"),
])
def test_bad_environment_is_not_silently_unlimited(monkeypatch, tmp_path, name, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.from_env()
