import re
from pathlib import Path

from sag.config.prompt_loader import load_react_engine_prompts


REPO_ROOT = Path(__file__).resolve().parents[1]
REACT_ENGINE_PATH = REPO_ROOT / "src/sag/agent/react_engine.py"
PROMPT_REF_RE = re.compile(
    r"# Prompt: (?P<path>src/sag/config/prompts/react_engine\.yaml):(?P<line>\d+) (?P<key>[\w.]+)"
)


def test_react_engine_prompt_reference_comments_resolve():
    source = REACT_ENGINE_PATH.read_text()
    refs = list(PROMPT_REF_RE.finditer(source))
    assert refs

    prompts = load_react_engine_prompts()

    for ref in refs:
        prompt_path = REPO_ROOT / ref.group("path")
        line_number = int(ref.group("line"))
        key = ref.group("key")

        assert prompt_path.exists()
        assert prompts.get(key).strip()

        lines = prompt_path.read_text().splitlines()
        assert 1 <= line_number <= len(lines)
        nearby = "\n".join(lines[max(0, line_number - 4) : min(len(lines), line_number + 3)])
        assert key.split(".")[-1] in nearby
