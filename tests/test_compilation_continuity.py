"""Small physical-file ablations; the synthetic launcher is not a Java benchmark."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from sag.benchmark.compilation_evidence import capture, validate_plans
from sag.benchmark.evaluator import evaluate
from sag.benchmark.recorder import reference, write_json
from sag.benchmark.requirements import bound_file, canonical_digest
from sag.benchmark.sag_observer import SAGRequirementObserver
from test_benchmark_recorder import setup
from test_sag_requirement_observer import LocalControl


@pytest.fixture
def compiled(setup, tmp_path, monkeypatch):
    root, make = setup
    for name in (
        "MAVEN_ARGS",
        "MAVEN_CONFIG",
        "MAVEN_PROJECTBASEDIR",
        "MAVEN_OPTS",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAVEN_SKIP_RC", "true")
    (root / "src").mkdir()
    (root / "src/Demo.java").write_text("class Demo {}")
    launcher = tmp_path / "mvn"
    launcher.write_text(f"#!{sys.executable}\n" + """
import sys
from pathlib import Path
if '--version' in sys.argv:
    print('Apache Maven 3.9.16\\nJava version: 17.0.12\\nJava home: /jdk')
else:
    print('[INFO] --- compiler:1.0:compile (default) @ tiny ---')
    if 'install' in sys.argv:
        p=Path('target/classes/Demo.class');p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(bytes.fromhex('cafebabe0000003d') + b'synthetic protocol fixture')
        print('[INFO] Compiling 1 source file')
    else:
        print('[INFO] Nothing to compile - all classes are up to date')
    print('[INFO] BUILD SUCCESS')
""")
    launcher.chmod(0o755)

    def task_change(task):
        task["steps"] = [
            {
                "id": name,
                "runner": "maven",
                "argv": ["mvn", goal],
                "cwd": ".",
                "java_major": 17,
                "maven_version": "3.9.16",
            }
            for name, goal in (("first", "install"), ("second", "test"))
        ]

    def spec_change(spec):
        row = {
            "kind": "compile",
            "subtype": "production",
            "module": "tiny",
            "scope": {"status": "declared", "module_path": "."},
            "depends_on": [],
            "dependencies_complete": True,
            "validation": {
                "rule": "native_goal",
                "goals": ["compiler:compile"],
                "plan_resolved": True,
                "position": 0,
            },
        }
        spec["requirements"] = [
            {**deepcopy(row), "id": step + "-compile", "step_id": step}
            for step in ("first", "second")
        ]
        spec["compilation_reuse"] = [
            {
                "producer_requirement": "first-compile",
                "consumer_requirement": "second-compile",
                "inputs": ["pom.xml", "src"],
                "outputs": ["target/classes"],
                "review_basis": "Synthetic single-source compiler fixture",
                "configuration_equivalence_basis": "The synthetic compiler receives identical configuration in both commands",
            }
        ]

    recorder = make(task_change=task_change, spec_change=spec_change)
    recorder.start()
    recorder.step("first", timeout=5, maven_bin=launcher)
    return root, recorder, launcher


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "remove_proof",
        "changed_source",
        "changed_class",
        "missing_class",
        "foreign_invocation",
        "missing_input_inventory",
        "forged_positive",
        "changed_runtime",
        "changed_compiler",
        "producer_not_compiled",
        "producer_timeout",
        "bad_hash",
    ],
)
def test_reuse_requires_compiler_and_same_attempt_physical_continuity(compiled, mutation):
    root, recorder, launcher = compiled
    if mutation == "changed_source":
        (root / "src/Demo.java").write_text("class Demo { int changed; }")
    elif mutation == "changed_class":
        (root / "target/classes/Demo.class").write_bytes(
            bytes.fromhex("cafebabe0000003d") + b"other"
        )
    elif mutation == "missing_class":
        (root / "target/classes/Demo.class").unlink()
    recorder.step("second", timeout=5, maven_bin=launcher)
    run = recorder.close()
    records = [json.loads(bound_file(recorder.base, ref).read_text()) for ref in run["invocations"]]
    second = records[1]
    if mutation in {"remove_proof", "forged_positive"}:
        second.pop("compilation_evidence")
        if mutation == "forged_positive":
            second["verified_compilation_reuse"] = {"second-compile": {"evidence_refs": []}}
    elif mutation in {"foreign_invocation", "missing_input_inventory", "bad_hash"}:
        ref = second["compilation_evidence"]["before"]
        path = recorder.base / ref["path"]
        value = json.loads(path.read_text())
        if mutation == "foreign_invocation":
            value["invocation_id"] = "another-run"
        else:
            value["observations"]["second-compile"].pop("inputs")
        write_json(path, value)
        if mutation != "bad_hash":
            second["compilation_evidence"]["before"] = reference(recorder.base, path)
    elif mutation == "changed_runtime":
        probe = second["runtime"]["launcher_probe"]
        path = bound_file(recorder.base, probe)
        path.write_text(path.read_text().replace("17.0.12", "17.0.13"))
        second["runtime"]["launcher_probe"].update(reference(recorder.base, path))
    elif mutation in {"changed_compiler", "producer_not_compiled"}:
        record = second if mutation == "changed_compiler" else records[0]
        path = bound_file(recorder.base, record["log"])
        text = (
            path.read_text().replace("compiler:1.0", "compiler:2.0")
            if mutation == "changed_compiler"
            else path.read_text().replace(
                "Compiling 1 source file", "Nothing to compile - all classes are up to date"
            )
        )
        path.write_text(text)
        record["log"] = reference(recorder.base, path)
    elif mutation == "producer_timeout":
        records[0].update(status="timeout", exit_code=137)
    for old, record in zip(run["invocations"], records):
        path = recorder.base / old["path"]
        write_json(path, record)
        old.update(reference(recorder.base, path))
    result = evaluate(recorder.task, recorder.spec, run, recorder.base)
    row = next(r for r in result["requirements"] if r["id"] == "second-compile")
    assert (row["status"] == "passed") == (mutation is None), row
    if mutation is None:
        assert row["reason"] == "same_attempt_compiled_output_reused"
        assert len(row["compilation_reuse"]["evidence_refs"]) == 4


def test_clean_observer_and_portable_capture_the_same_compiler_scope(compiled, tmp_path):
    root, recorder, _ = compiled
    control = LocalControl(root)
    observer = SAGRequirementObserver(
        tmp_path / "session", "run-1", str(root), recorder.task, recorder.spec, control.execute
    )
    scope, errors = observer._scope(recorder.task["steps"][1])
    assert not errors
    observed = observer._snapshot(scope)
    assert not observed["errors"]
    assert observed["compilation"] == capture(root, recorder.spec["compilation_reuse"])


@pytest.mark.parametrize("mutation", [None, "missing_proof", "changed_class", "changed_source", "other_message"])
def test_cache_load_uses_the_same_physical_producer_proof(compiled, mutation):
    root, recorder, launcher = compiled
    text = launcher.read_text().replace(
        "Nothing to compile - all classes are up to date",
        "Loaded from the build cache, saving 0.629s" if mutation != "other_message" else "Cache entry was found",
    )
    launcher.write_text(text)
    if mutation == "changed_class":
        (root / "target/classes/Demo.class").write_bytes(bytes.fromhex("cafebabe0000003d") + b"different cache bytes")
    elif mutation == "changed_source":
        (root / "src/Demo.java").write_text("class Demo { int different; }")
    recorder.step("second", timeout=5, maven_bin=launcher)
    run = recorder.close()
    if mutation == "missing_proof":
        ref = run["invocations"][1]
        path = bound_file(recorder.base, ref)
        record = json.loads(path.read_text())
        record.pop("compilation_evidence")
        write_json(path, record)
        ref.update(reference(recorder.base, path))
    scored = evaluate(recorder.task, recorder.spec, run, recorder.base)
    row = next(r for r in scored["requirements"] if r["id"] == "second-compile")
    assert (row["status"] == "passed") == (mutation is None), row
    if mutation is None:
        assert row["reason"] == "same_attempt_compiled_output_reused"
        assert len(row["compilation_reuse"]["evidence_refs"]) == 4


@pytest.mark.parametrize(
    "mutation",
    ["reversed", "runtime", "goal", "module", "overlap", "escape", "missing_pom", "duplicate"],
)
def test_reuse_scope_cannot_silently_change_task_or_compiler(compiled, mutation):
    _, recorder, _ = compiled
    task, spec = deepcopy(recorder.task), deepcopy(recorder.spec)
    plan = spec["compilation_reuse"][0]
    if mutation == "reversed":
        plan["producer_requirement"], plan["consumer_requirement"] = (
            plan["consumer_requirement"],
            plan["producer_requirement"],
        )
    elif mutation == "runtime":
        task["steps"][1]["java_major"] = 21
    elif mutation == "goal":
        spec["requirements"][1]["validation"]["goals"] = ["compiler:testCompile"]
    elif mutation == "module":
        spec["requirements"][1]["module"] = "other"
    elif mutation == "overlap":
        plan["inputs"].append("target")
    elif mutation == "escape":
        plan["outputs"] = ["../classes"]
    elif mutation == "missing_pom":
        plan["inputs"].remove("pom.xml")
    else:
        spec["compilation_reuse"].append(deepcopy(plan))
    with pytest.raises(ValueError):
        validate_plans(spec, task)
