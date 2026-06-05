from sag.config.settings import Config


def test_default_thinking_model_is_gpt54_mini():
    config = Config()

    assert config.thinking_model == "gpt-5.4-mini"
    assert config.thinking_provider == "openai"
    assert config.get_litellm_model_name("thinking") == "gpt-5.4-mini"
    assert config.is_gpt5_model("thinking") is True


def test_from_env_uses_gpt54_mini_thinking_default_without_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SAG_THINKING_MODEL", raising=False)
    monkeypatch.delenv("SAG_THINKING_PROVIDER", raising=False)

    config = Config.from_env()

    assert config.thinking_model == "gpt-5.4-mini"
    assert config.thinking_provider == "openai"


def test_default_docker_base_image_is_ubuntu_2404():
    config = Config()

    assert config.docker_base_image == "ubuntu:24.04"


def test_from_env_uses_ubuntu_2404_docker_default_without_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SAG_DOCKER_BASE_IMAGE", raising=False)

    config = Config.from_env()

    assert config.docker_base_image == "ubuntu:24.04"


def test_from_env_allows_docker_base_image_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAG_DOCKER_BASE_IMAGE", "ubuntu:26.04")

    config = Config.from_env()

    assert config.docker_base_image == "ubuntu:26.04"
