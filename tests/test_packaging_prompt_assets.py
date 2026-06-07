import zipfile
from pathlib import Path

import pytest


def test_built_wheel_contains_react_engine_prompt_yaml():
    dist_dir = Path("dist")
    wheels = sorted(dist_dir.glob("setup_agent-*.whl"))
    if not wheels:
        pytest.skip("Build a wheel before running this packaging asset test")

    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())

    assert "sag/config/prompts/react_engine.yaml" in names
