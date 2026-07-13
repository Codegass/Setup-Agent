"""GradleBackend install verb: publish to the local maven repo when possible.

Live bigtop evidence (probe 2026-07-13): the island guidance recommended gradle
'publishToMavenLocal' for bigtop-data-generators, the agent obeyed with
build(action='install') — but the backend mapped install -> assemble, so the
jars landed in build/libs and were NEVER published to ~/.m2. The dependent
island (bigpetstore-transaction-queue) then failed resolving the SNAPSHOT
artifact, 13 times. The rendering layer was right; the execution layer was not.

Contract: install -> publishToMavenLocal when the project applies the
maven-publish plugin (probed from the working directory's gradle build files),
else assemble (publishToMavenLocal would fail without the plugin).
"""

from sag.tools.build.backends import GradleBackend


class FakeGradleTool:
    """Records execute() kwargs; answers build-file probes via `files`."""

    def __init__(self, files: dict):
        self.files = files  # path -> content
        self.calls = []

    class _Orch:
        def __init__(self, files):
            self.files = files

        def execute_command(self, cmd, workdir=None, timeout=None, **_):
            # Answer `cat <path>` probes for gradle build files.
            if cmd.startswith("cat "):
                path = cmd.split("cat ", 1)[1].split(" ", 1)[0].strip()
                if path in self.files:
                    return {"success": True, "exit_code": 0, "output": self.files[path]}
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": ""}

    @property
    def orchestrator(self):
        return self._Orch(self.files)

    def execute(self, **kwargs):
        self.calls.append(kwargs)

        class R:
            success = True
            output = "ok"

        return R()


def test_install_publishes_when_maven_publish_plugin_applied():
    tool = FakeGradleTool({
        "/workspace/proj/build.gradle": "apply plugin: 'maven-publish'\n",
    })
    GradleBackend(tool).run("install", None, "/workspace/proj", None)
    assert tool.calls[0]["tasks"] == "publishToMavenLocal"


def test_install_publishes_for_plugins_block_syntax():
    tool = FakeGradleTool({
        "/workspace/proj/build.gradle.kts": 'plugins { `maven-publish` }\n',
    })
    GradleBackend(tool).run("install", None, "/workspace/proj", None)
    assert tool.calls[0]["tasks"] == "publishToMavenLocal"


def test_install_detects_plugin_in_subprojects_block_of_root_build():
    # bigtop-data-generators shape: the multi-project root's build.gradle
    # applies maven-publish inside subprojects { } for all children.
    tool = FakeGradleTool({
        "/workspace/proj/build.gradle": "subprojects {\n  apply plugin: 'maven-publish'\n}\n",
    })
    GradleBackend(tool).run("install", None, "/workspace/proj", None)
    assert tool.calls[0]["tasks"] == "publishToMavenLocal"


def test_install_falls_back_to_assemble_without_plugin():
    tool = FakeGradleTool({
        "/workspace/proj/build.gradle": "apply plugin: 'java'\n",
    })
    GradleBackend(tool).run("install", None, "/workspace/proj", None)
    assert tool.calls[0]["tasks"] == "assemble"


def test_install_falls_back_to_assemble_when_probe_fails():
    tool = FakeGradleTool({})  # no readable build files
    GradleBackend(tool).run("install", None, "/workspace/proj", None)
    assert tool.calls[0]["tasks"] == "assemble"


def test_other_verbs_unchanged():
    tool = FakeGradleTool({
        "/workspace/proj/build.gradle": "apply plugin: 'maven-publish'\n",
    })
    backend = GradleBackend(tool)
    for verb, task in (("test", "test"), ("package", "assemble"), ("compile", "compileJava")):
        tool.calls.clear()
        backend.run(verb, None, "/workspace/proj", None)
        assert tool.calls[0]["tasks"] == task, verb
