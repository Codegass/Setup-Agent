import re
from pathlib import Path

from sag.config.prompt_loader import load_react_engine_prompts

REPO_ROOT = Path(__file__).resolve().parents[1]
REACT_PROMPT_SOURCE_PATHS = (
    REPO_ROOT / "src/sag/agent/react_engine.py",
    REPO_ROOT / "src/sag/agent/react_prompt_builder.py",
)
PROMPT_REF_RE = re.compile(
    r"# Prompt: (?P<path>src/sag/config/prompts/react_engine\.yaml):(?P<line>\d+) (?P<key>[\w.]+)"
)
PROMPT_LOOKUP_RE = re.compile(
    r"self\.prompts\.(?:get|format)\(\s*[\"'](?P<key>[\w.]+)[\"']", re.DOTALL
)


def test_react_engine_prompt_reference_comments_resolve():
    sources = {path: path.read_text() for path in REACT_PROMPT_SOURCE_PATHS}
    refs = [
        (path, ref) for path, source in sources.items() for ref in PROMPT_REF_RE.finditer(source)
    ]
    assert refs

    prompts = load_react_engine_prompts()
    for path, source in sources.items():
        lines = source.splitlines()
        for lookup in PROMPT_LOOKUP_RE.finditer(source):
            key = lookup.group("key")
            lookup_line = source[: lookup.start()].count("\n")
            nearby_source = "\n".join(lines[max(0, lookup_line - 3) : lookup_line + 1])
            nearby_refs = list(PROMPT_REF_RE.finditer(nearby_source))

            assert any(ref.group("key") == key for ref in nearby_refs), (
                f"{path.relative_to(REPO_ROOT)} lookup for {key} is missing a nearby "
                "# Prompt: reference"
            )

    for _, ref in refs:
        prompt_path = REPO_ROOT / ref.group("path")
        line_number = int(ref.group("line"))
        key = ref.group("key")

        assert prompt_path.exists()
        assert prompts.get(key).strip()

        lines = prompt_path.read_text().splitlines()
        assert 1 <= line_number <= len(lines)
        assert lines[line_number - 1].lstrip().startswith(f"{key.split('.')[-1]}:")
