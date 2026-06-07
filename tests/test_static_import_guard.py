from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "src" / "sag"

DISALLOWED_IMPORT_PREFIXES = (
    "from agent",
    "import agent",
    "from config",
    "import config",
    "from docker_orch",
    "import docker_orch",
    "from reporting",
    "import reporting",
    "from testcases",
    "import testcases",
    "from tools",
    "import tools",
    "from ui",
    "import ui",
)


def test_runtime_code_does_not_use_old_absolute_imports():
    offenders = []

    for path in RUNTIME_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(DISALLOWED_IMPORT_PREFIXES):
                offenders.append(f"{path.relative_to(RUNTIME_ROOT)}:{line_number}: {stripped}")

    assert offenders == []
