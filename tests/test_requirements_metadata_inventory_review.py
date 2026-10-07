"""Portable reactor-preparation fixtures exercise provenance and authority.

These are synthetic archives. They need neither the developer's output tree nor
Maven, Docker, Java, a source checkout, or network access.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.build_benchmark_requirements import task_digest
from scripts.requirements_metadata_inventory import attach_preparation_inventory
from sag.benchmark.requirements import bound_file


def archive_fixture(tmp_path):
    prep = tmp_path / "preparation"
    out = tmp_path / "package"
    project, commit = "reactor-fixture", "b" * 40
    workspace = "/worktrees/" + project
    launcher = "/opt/apache-maven-3.9.16/bin/mvn"
    options = ["-B", "-f", "pom.xml", "-Pci", "-pl", ".,child", "-Dfeature=true"]
    step = {"id": "ci-step-1", "runner": "maven", "argv": ["mvn", *options, "clean", "install"],
            "cwd": ".", "java_major": 17, "maven_version": "3.9.16", "environment": {}}
    task = {"schema_version": 1, "repo": "example/fixture", "sha": commit, "steps": [step]}
    spec = {"project_id": project, "task_sha256": task_digest(task),
            "annotation_completeness": {"status": "review_required", "gaps": ["lifecycle_unreviewed"]},
            "requirements": [{"id": "unknown-plan", "kind": "unclassified", "module": None}],
            "steps": [{"id": step["id"], "execution_plan_resolved": False, "modules": []}]}

    def put(name, raw):
        if isinstance(raw, str):
            raw = raw.encode()
        path = prep / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def save(name, value):
        return put(name, json.dumps(value, indent=2) + "\n")

    def model(aid, module, packaging="jar"):
        base = workspace + ("/" + module if module != "." else "")
        return (f"<project><groupId>org.example</groupId><artifactId>{aid}</artifactId>"
                f"<version>1</version><packaging>{packaging}</packaging><build>"
                f"<directory>{base}/target</directory><outputDirectory>{base}/target/classes</outputDirectory>"
                f"<finalName>{aid}-1</finalName></build></project>")

    # A tracked test-fixture POM is deliberately not in the effective reactor.
    sources = [("pom.xml", "parent"), ("child/pom.xml", "child"), ("fixtures/pom.xml", "fixture")]
    source_rows = []
    for name, aid in sources:
        source_rows.append({"source_path": name, "kind": "file", "pinned_bytes_equal": True,
                            "git_show_argv": ["git", "show", commit + ":" + name], "git_show_exit_code": 0,
                            **put(project + "/source-poms/" + name,
                                  f"<project><artifactId>{aid}</artifactId></project>")})
    save(project + "/source-poms/index.json", {
        "project": project, "repo": task["repo"], "commit": commit, "head_observed": commit,
        "status": "captured", "files": source_rows, "inventory_exit_code": 0,
        "inventory_command": ["git", "-C", workspace, "ls-files", "-z", "--", "pom.xml", "**/pom.xml"],
        "inventory_stdout": put(project + "/source-poms/tracked-files.raw", "\0".join(n for n, _ in sources) + "\0"),
        "inventory_stderr": put(project + "/source-poms/tracked-files.stderr", ""),
    })
    outputs = {
        "effective-pom": put(project + "/effective-pom.xml", "<projects>" + model("parent", ".", "pom") + model("child", "child") + "</projects>"),
        "effective-settings": put(project + "/effective-settings.xml", "<settings><localRepository>/isolated/metadata-repository</localRepository></settings>"),
    }
    runtime = put(project + "/runtime.txt", "Apache Maven 3.9.16\nJava version: 17.0.20.1, vendor: Ubuntu, runtime: /usr/lib/jvm/java-17-openjdk-amd64\n")
    commands = []
    for goal in ("effective-pom", "effective-settings"):
        commands.append({"argv": [launcher, *options, "org.apache.maven.plugins:maven-help-plugin:3.5.2:" + goal,
                                  "-Dverbose", "-Doutput=/metadata/preparation/" + outputs[goal]["path"]],
                         "cwd": workspace, "exit_code": 0, "status": "completed", "elapsed_seconds": 0.1,
                         "log": put(project + "/" + goal + ".log", "[INFO] BUILD SUCCESS\n")})
    state = {"id": step["id"], "frozen_argv": step["argv"], "status": "captured_review_required",
             "java_major": 17, "maven_version_constraint": "3.9.16", "launcher": launcher,
             "metadata_options": options, "observed_java_major": 17, "observed_maven_version": "3.9.16",
             "metadata_environment": {"JAVA_HOME": "/usr/lib/jvm/java-17-openjdk-amd64", "MAVEN_OPTS": "-Xmx1024m", "MAVEN_SKIP_RC": "true"},
             "runtime_probe": {"argv": [launcher, "--version"], "cwd": workspace, "exit_code": 0,
                               "status": "completed", "elapsed_seconds": 0.1, "log": runtime},
             "commands": commands, "outputs": outputs}
    launcher_input = put(project + "/launcher-inputs/.mvn/jvm.config", "-Xmx1024m\n")
    launcher_index = save(project + "/launcher-inputs/index.json", {
        "commit": commit,
        "tracked_inventory": put(project + "/launcher-inputs/tracked-files.raw", ".mvn/jvm.config\0"),
        "tracked_inventory_stderr": put(project + "/launcher-inputs/tracked-files.stderr", ""),
        "files": [{"source_path": ".mvn/jvm.config", "kind": "file", "pinned_bytes_equal": True, **launcher_input}],
        "absent_tracked_paths": ["mvnw", ".mvn/maven.config", ".mvn/wrapper/maven-wrapper.properties"],
        "scope": "git tracked launcher/config paths at pinned commit; not a wrapper approval",
    })
    status = {"project": project, "repo": task["repo"], "commit": commit,
              "status": "captured_review_required", "metadata_only": True, "steps": [state],
              "launcher_inputs": {"status": "captured", "index": launcher_index},
              "preparation_script": put("runner-sources/fixture.py", "# Synthetic trusted metadata recorder fixture.\n")}
    status_name = project + "/metadata-status.json"
    save(status_name, status)
    save("request-" + project + ".json", {"project_id": project, "repo": task["repo"], "commit": commit,
                                         "original_task": step, "original_steps": [step],
                                         "cache_policy": "isolated_metadata_only_not_shared_with_agent"})
    put(project + "/HEAD.txt", commit + "\n")
    put(project + "/tracked-diff.txt", "")
    put(project + "/untracked.txt", "")
    return prep, out, spec, task, status, state, put, save, status_name


def test_attach_reactor_archive_binds_sources_without_granting_task_authority(tmp_path):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    before = deepcopy(spec)
    result = attach_preparation_inventory(spec, task, prep, out)
    audit = result.pop("metadata_preparation")
    assert result == spec == before
    assert audit["status"] == "captured_for_review"
    assert audit["authorizes_complete"] is False
    inventory = audit["steps"][0]["inventory"]
    assert inventory["model_count"] == 2  # Not the three captured source POMs.
    assert "not proof of executed reactor" in inventory["scope"]
    parent, child = inventory["models"]
    assert parent["artifacts"] == []
    assert parent["installs"][0]["repository_relative_path"] == "org/example/parent/1/parent-1.pom"
    assert child["module_path"] == "child"
    assert child["artifacts"][0]["path"] == "child/target/child-1.jar"
    assert child["provenance"]["task_sha256"] == task_digest(task)
    assert child["provenance"]["argv"] == state["commands"][0]["argv"]
    assert child["provenance"]["source_reactor_sha256"] == state["outputs"]["effective-pom"]["sha256"]
    for source in audit["sources"].values():
        assert bound_file(out, source).is_file()
    assert bound_file(out, audit["sources"]["launcher_input:.mvn/jvm.config"]).read_bytes() == b"-Xmx1024m\n"
    assert audit["steps"][0]["gaps"]


def test_attach_corrupt_source_archive_is_rejected(tmp_path):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    (prep / "reactor-fixture/source-poms/child/pom.xml").write_text("changed")
    with pytest.raises(ValueError):
        attach_preparation_inventory(spec, task, prep, out)


def test_attach_dirty_metadata_cannot_claim_inventory(tmp_path):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    put("reactor-fixture/tracked-diff.txt", "pom.xml\n")
    result = attach_preparation_inventory(spec, task, prep, out)
    assert result["metadata_preparation"]["status"] == "partial_or_unavailable"
    assert "inventory" not in result["metadata_preparation"]["steps"][0]


def test_attach_unresolved_artifact_is_retained_without_upgrading_definition(tmp_path):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    ref = state["outputs"]["effective-pom"]
    raw = (prep / ref["path"]).read_text().replace(
        "<finalName>child-1</finalName>",
        "<finalName>child-1</finalName><plugins><plugin><artifactId>maven-shade-plugin</artifactId></plugin></plugins>",
    )
    state["outputs"]["effective-pom"] = put(ref["path"], raw)
    save(name, status)
    result = attach_preparation_inventory(spec, task, prep, out)
    assert result["annotation_completeness"] == spec["annotation_completeness"]
    assert result["requirements"] == spec["requirements"]
    child = result["metadata_preparation"]["steps"][0]["inventory"]["models"][1]
    assert child["resolution_status"] == "unresolved"
    assert "artifact_plugin_requires_review:maven-shade-plugin" in child["exceptions"]


@pytest.mark.parametrize("mutation", ["profile", "module_selection", "property", "goal", "cwd", "launcher"])
def test_attach_actual_metadata_invocation_must_preserve_frozen_configuration(tmp_path, mutation):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    command = state["commands"][1]
    if mutation == "profile":
        command["argv"].remove("-Pci")
    elif mutation == "module_selection":
        command["argv"][command["argv"].index("-pl") + 1] = "."
    elif mutation == "property":
        command["argv"].remove("-Dfeature=true")
    elif mutation == "goal":
        command["argv"][-3] = "install"
    elif mutation == "cwd":
        command["cwd"] += "/other"
    else:
        command["argv"][0] = "/other/mvn"
    save(name, status)
    with pytest.raises(ValueError):
        attach_preparation_inventory(spec, task, prep, out)


def test_attach_runtime_uses_raw_probe_not_duplicated_summary_field(tmp_path):
    prep, out, spec, task, status, state, put, save, name = archive_fixture(tmp_path)
    state["runtime_probe"]["log"] = put("reactor-fixture/runtime.txt", "Apache Maven 3.9.16\nJava version: 1.8.0_504, vendor: Ubuntu\n")
    save(name, status)
    with pytest.raises(ValueError):
        attach_preparation_inventory(spec, task, prep, out)
