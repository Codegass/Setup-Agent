"""Native extension inputs are reviewed by bytes, never by repository name."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts.build_benchmark_requirements import (
    import_jvm_config_review,
    pom_execution_bindings,
    reviewed_install_poms,
    reviewed_plugin_descriptors,
)
from sag.benchmark import jvm_inputs
from sag.benchmark.sag_observer import SAGRequirementObserver
from test_benchmark_recorder import setup  # real tiny Git checkout


def ref(base, path):
    raw = path.read_bytes()
    return {
        "path": str(path.relative_to(base)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def jvm_review(commit, raw):
    tokens = jvm_inputs.literal_tokens(raw)
    return {
        "policy": "pinned-maven-jvm-config-v1",
        "source_commit": commit,
        "source_path": ".mvn/jvm.config",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "tokens": tokens,
        "reviewed_by": "regression fixture",
        "review_basis": "Exact original source input; task requirements retain its effects",
        "properties": [
            {"argument": a, "effect": "runtime_property", "basis": "Fixture source review"}
            for a in tokens
            if a.startswith("-D")
        ],
    }


def test_real_tycho_descriptor_corrects_nonconventional_prefix(tmp_path):
    from sag.benchmark.native_evidence import maven_events

    jar = Path(__file__).parent / "fixtures/tycho-p2-plugin-5.0.3.jar"
    dest = tmp_path / jar.name
    dest.write_bytes(jar.read_bytes())
    model = ET.fromstring("""<project><build><plugins><plugin><groupId>org.eclipse.tycho</groupId>
      <artifactId>tycho-p2-plugin</artifactId><version>5.0.3</version><executions><execution>
      <id>default-p2-metadata-default</id><phase>package</phase><goals><goal>p2-metadata-default</goal></goals>
      </execution></executions></plugin></plugins></build></project>""")
    review = {"plugin_descriptors": [ref(tmp_path, dest)]}
    prefixes, files = reviewed_plugin_descriptors(review, tmp_path, {"module": model})
    assert pom_execution_bindings(model)[0]["goal"] == "tycho-p2:p2-metadata-default"
    assert (
        pom_execution_bindings(model, prefixes)[0]["goal"] == "tycho-p2-plugin:p2-metadata-default"
    )
    events = maven_events(
        "[INFO] --- tycho-p2-plugin:5.0.3:p2-metadata-default (default) @ module ---\n[INFO] BUILD SUCCESS\n",
        terminal=True,
        serial=True,
    )
    assert events[0]["goal"] == pom_execution_bindings(model, prefixes)[0]["goal"]
    assert list(files.values()) == [dest]
    model.find("build/plugins/plugin/version").text = "5.0.2"
    with pytest.raises(ValueError):
        reviewed_plugin_descriptors(review, tmp_path, {"module": model})
    model.find("build/plugins/plugin/version").text = "5.0.3"
    review["plugin_descriptors"] *= 2
    with pytest.raises(ValueError):
        reviewed_plugin_descriptors(review, tmp_path, {"module": model})
    review["plugin_descriptors"].pop()
    dest.write_bytes(dest.read_bytes() + b"tampered")
    with pytest.raises(ValueError):
        reviewed_plugin_descriptors(review, tmp_path, {"module": model})


@pytest.mark.parametrize(
    "mutation", [None, "removed", "changed", "extra_property", "environment", "missing_review"]
)
def test_direct_recorders_agree_on_reviewed_jvm_config(setup, tmp_path, mutation):
    root, make = setup
    raw = b"--add-exports=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED\n-Djdk.xml.totalEntitySizeLimit=0\n"

    def maven(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "test"], java_major=17)

    recorder = make(task_change=maven)
    step = recorder.task["steps"][0]
    declaration = jvm_review(recorder.task["sha"], raw)
    recorder.spec["steps"] = [{"step_id": step["id"], "jvm_config_review": declaration}]
    if mutation == "removed":
        raw = None
    elif mutation == "changed":
        raw = raw.replace(b"Limit=0", b"Limit=1")
    elif mutation == "extra_property":
        raw += b"-DskipTests=true\n"
    elif mutation == "missing_review":
        recorder.spec["steps"] = []
    env = {
        "MAVEN_SKIP_RC": "true",
        "MAVEN_OPTS": "-DskipTests" if mutation == "environment" else "",
    }
    if raw is not None:
        (root / ".mvn").mkdir()
        (root / ".mvn/jvm.config").write_bytes(raw)
    out = recorder.base / "input-observation"
    out.mkdir(parents=True)
    portable = recorder._effective_execution(step, env, out)
    observer = SAGRequirementObserver(
        tmp_path / "session", "run-1", str(root), recorder.task, recorder.spec, lambda *_: None
    )
    snapshot = {
        "configs": [],
        "environment_observed": True,
        "runtime_inputs": env,
        "startup_rc_present": False,
    }
    if raw is not None:
        snapshot["configs"] = [
            {
                "path": ".mvn/jvm.config",
                "present": True,
                "data": base64.b64encode(raw).decode(),
                "fingerprint": {"sha256": hashlib.sha256(raw).hexdigest()},
            }
        ]
    live = observer._inputs(step, snapshot)
    assert portable["inputs_complete"] == live["inputs_complete"] == (mutation is None)


@pytest.mark.parametrize(
    "mutation", [None, "untracked", "wrong_commit", "wrong_hash", "unreviewed_property", "argfile"]
)
def test_jvm_review_import_requires_exact_pinned_source(tmp_path, mutation):
    raw = b"-Duser.language=en\n"
    path = tmp_path / "jvm.config"
    if mutation == "argfile":
        raw += b"@hidden.args\n"
    path.write_bytes(raw)
    task = {"sha": "a" * 40}
    declaration = jvm_review(task["sha"], b"-Duser.language=en\n")
    source = ref(tmp_path, path)
    index = {
        "commit": task["sha"],
        "files": [
            {**source, "source_path": ".mvn/jvm.config", "kind": "file", "pinned_bytes_equal": True}
        ],
    }
    if mutation == "untracked":
        index["files"][0]["pinned_bytes_equal"] = False
    elif mutation == "wrong_commit":
        index["commit"] = "b" * 40
    elif mutation == "wrong_hash":
        declaration["sha256"] = "0" * 64
    elif mutation == "unreviewed_property":
        declaration["properties"] = []
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index))
    review = {
        "jvm_config_review": declaration,
        "additional_sources": {
            "launcher_inventory": ref(tmp_path, index_path),
            "launcher_source:.mvn/jvm.config": source,
        },
    }
    if mutation is None:
        result, sources = import_jvm_config_review(review, tmp_path, task, tmp_path / "out")
        assert result == declaration and len(sources) == 2
    else:
        with pytest.raises(ValueError):
            import_jvm_config_review(review, tmp_path, task, tmp_path / "out")


@pytest.mark.parametrize(
    "mutation",
    [None, "late_producer", "wrong_module", "outside_path", "no_source", "tampered_source"],
)
def test_generated_consumer_pom_is_an_install_obligation_not_a_persistent_package(
    tmp_path, mutation
):
    source = tmp_path / "producer-source.java"
    source.write_text(
        "// fixture: producer replaces MavenProject file and deletes temporary POM on exit\n"
    )
    producer = {
        "goal": "producer:consumer-pom",
        "execution": "default",
        "occurrence": 0,
        "position": 1,
        "version": "1",
        "module": "library",
    }
    review = {
        "generated_install_poms": [
            {
                "module": "library",
                "path": "lib/.consumer-pom.xml",
                "producer": deepcopy(producer),
                "producer_sources": [ref(tmp_path, source)],
                "review_basis": "Reviewed temporary source; installed POM is the durable obligation",
            }
        ]
    }
    row = {
        "kind": "install",
        "module": "library",
        "validation": {
            "goals": ["install:install"],
            "position": 2,
            "execution": "default",
            "occurrence": 0,
        },
        "expectations": {"artifacts": [{"role": "installed_pom"}]},
    }
    entry = review["generated_install_poms"][0]
    if mutation == "late_producer":
        row["validation"]["position"] = 0
    elif mutation == "wrong_module":
        entry["module"] = "other"
    elif mutation == "outside_path":
        entry["path"] = "../pom.xml"
    elif mutation == "no_source":
        entry["producer_sources"] = []
    elif mutation == "tampered_source":
        source.write_text("changed")
    args = (
        review,
        tmp_path,
        [{"id": "library", "path": "lib"}],
        [producer],
        [row],
        tmp_path / "out",
    )
    if mutation is None:
        paths, refs = reviewed_install_poms(*args)
        assert paths == {"library": "lib/.consumer-pom.xml"} and refs
        assert row["validation"]["goals"] == ["producer:consumer-pom", "install:install"]
        assert not list(tmp_path.rglob(".consumer-pom.xml"))
    else:
        with pytest.raises(ValueError):
            reviewed_install_poms(*args)
