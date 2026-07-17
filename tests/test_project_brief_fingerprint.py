import json

from sag.agent.project_brief import (
    ProjectBriefComposer,
    ProjectBriefInputs,
    ProjectBriefStore,
)


def _inputs(**updates):
    values = {
        "manifest": {"pom.xml": "<jdk>8</jdk>"},
        "detected_toolchain": {"java": "17", "maven": "3.9"},
        "submodule_state": (" abc123 third_party/lib",),
        "build_roots": ({"root": ".", "system": "maven", "goal": "install", "depends_on": []},),
        "repo_docs": {"README.md": "Use ./mvnw install"},
        "analyzer_version": "analyzer-v1",
        "composer_version": "composer-v1",
    }
    values.update(updates)
    return ProjectBriefInputs(**values)


def test_each_declared_dependency_invalidates_the_input_fingerprint():
    baseline = _inputs().fingerprint()
    variants = [
        _inputs(manifest={"pom.xml": "<jdk>11</jdk>"}),
        _inputs(detected_toolchain={"java": "8", "maven": "3.9"}),
        _inputs(submodule_state=("+def456 third_party/lib",)),
        _inputs(build_roots=({"root": "module", "system": "maven"},)),
        _inputs(repo_docs={"README.md": "Use ./mvnw verify"}),
        _inputs(analyzer_version="analyzer-v2"),
        _inputs(composer_version="composer-v2"),
    ]

    assert all(item.fingerprint() != baseline for item in variants)
    assert set(_inputs().component_fingerprints()) == {
        "manifest",
        "detected_toolchain",
        "submodule_state",
        "build_roots",
        "repo_docs",
        "analyzer_version",
        "composer_version",
    }


def test_matching_fingerprint_reuses_cached_complete_brief():
    composer = ProjectBriefComposer()
    first = composer.compose(_inputs(), [])
    cached = composer.compose(_inputs(), [], cached_brief=first)

    assert cached is first
    assert composer.composition_count == 1


class _AtomicFiles:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("cat > ") and " <<'" in command:
            first, body = command.split("\n", 1)
            path = first.split()[2]
            delimiter = first.rsplit("'", 2)[1]
            suffix = f"\n{delimiter}"
            assert body.endswith(suffix)
            self.files[path] = body[: -len(suffix)] + "\n"
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("truncate -s -1 "):
            path = command.split()[-1]
            self.files[path] = self.files[path][:-1]
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("mv "):
            _verb, source, target = command.split()
            self.files[target] = self.files.pop(source)
            return {"success": True, "exit_code": 0, "output": ""}
        if command.startswith("cat "):
            path = command.split()[1]
            if path not in self.files:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": self.files[path]}
        raise AssertionError(command)


def test_store_publishes_only_complete_validated_json_by_atomic_rename():
    orchestrator = _AtomicFiles()
    store = ProjectBriefStore(orchestrator)
    brief = ProjectBriefComposer().compose(_inputs(), [])

    store.write(brief)

    assert store.load() == brief
    assert not any(path.endswith(".tmp") for path in orchestrator.files)
    payload = json.loads(orchestrator.files[store.path])
    assert payload == brief.model_dump(mode="json")
    assert any(command.startswith("mv ") for command in orchestrator.commands)
