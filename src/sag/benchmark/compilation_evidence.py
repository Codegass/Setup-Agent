"""Physical continuity for explicitly reviewed, within-attempt compiler reuse.

The current compiler must still validate the module and report it up to date.
This is not a remote-cache proof or a compiler dependency resolver.
"""

from __future__ import annotations

import inspect
from pathlib import PurePosixPath

from .requirements import bound_file, canonical_digest, load_json


def plans_for(spec, step_id):
    rows = {r["id"]: r for r in spec["requirements"]}
    return [
        p
        for p in spec.get("compilation_reuse", [])
        if step_id
        in {rows[p[k]]["step_id"] for k in ("producer_requirement", "consumer_requirement")}
    ]


def validate_plans(spec, task):
    rows = {r["id"]: r for r in spec["requirements"]}
    steps = {s["id"]: (i, s) for i, s in enumerate(task["steps"])}
    seen = set()
    for plan in spec.get("compilation_reuse", []):
        producer = rows.get(plan.get("producer_requirement"), {})
        consumer = rows.get(plan.get("consumer_requirement"), {})
        if (
            not producer
            or not consumer
            or consumer["id"] in seen
            or any(
                r["kind"] != "compile" or r.get("validation", {}).get("rule") != "native_goal"
                for r in (producer, consumer)
            )
            or producer.get("module") != consumer.get("module")
            or producer.get("subtype") != consumer.get("subtype")
            or producer["validation"].get("goals") != consumer["validation"].get("goals")
            or not plan.get("review_basis")
            or not plan.get("configuration_equivalence_basis")
        ):
            raise ValueError(
                "Compiler reuse needs two matching compile obligations and an explicit input review"
            )
        before, after = steps[producer["step_id"]], steps[consumer["step_id"]]
        if (
            before[0] >= after[0]
            or any(s["runner"] != "maven" for _, s in (before, after))
            or any(
                before[1].get(k) != after[1].get(k) for k in ("cwd", "java_major", "maven_version")
            )
        ):
            raise ValueError("Compiler reuse requires earlier execution in the same frozen runtime")
        paths = []
        for kind in ("inputs", "outputs"):
            declared = plan.get(kind)
            if (
                not isinstance(declared, list)
                or not declared
                or len(set(declared)) != len(declared)
            ):
                raise ValueError(
                    "Compiler reuse needs explicit unique input and class-output paths"
                )
            for value in declared:
                p = PurePosixPath(value)
                if (
                    p.is_absolute()
                    or ".." in p.parts
                    or str(p) != value
                    or value == "."
                    or ".git" in p.parts
                ):
                    raise ValueError(
                        "Compiler continuity scope must stay within its reviewed checkout paths"
                    )
                paths.append(value)
        if len(set(paths)) != len(paths) or any(
            a.startswith(b + "/") or b.startswith(a + "/")
            for a in plan["inputs"]
            for b in plan["outputs"]
        ):
            raise ValueError("Compiler inputs and outputs must have separate scopes")
        module = consumer.get("scope", {}).get("module_path")
        if (
            not module
            or "pom.xml" not in plan["inputs"]
            or str(PurePosixPath(module) / "pom.xml") not in plan["inputs"]
        ):
            raise ValueError("Compiler continuity input review must include root and module models")
        seen.add(consumer["id"])


def capture(root, plans):
    """Only read reviewed paths. Return hashes and class headers, no source data."""
    import hashlib
    import os
    import pathlib
    import stat

    root = pathlib.Path(root)
    result = {}

    def safe(path):
        if not path.resolve().is_relative_to(root.resolve()) or any(
            p.is_symlink() for p in [path, *path.parents]
        ):
            raise ValueError("Symlink or path escape in compiler continuity scope")
        return path

    def files(paths, outputs):
        inventory = {"roots": {}, "files": {}}
        for relative in paths:
            path = safe(root / relative)
            inventory["roots"][relative] = (
                "directory" if path.is_dir() else "file" if path.is_file() else "absent"
            )
            if not path.exists():
                continue
            candidates = [path]
            if path.is_dir():
                candidates = []

                def fail(exc):
                    raise exc

                for current, dirs, names in os.walk(path, followlinks=False, onerror=fail):
                    for name in dirs:
                        safe(pathlib.Path(current) / name)
                    candidates.extend(safe(pathlib.Path(current) / name) for name in names)
            for item in candidates:
                if outputs and item.suffix != ".class":
                    continue
                before = item.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Nonregular compiler input/output")
                h = hashlib.sha256()
                with item.open("rb") as source:
                    header = source.read(8)
                    h.update(header)
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        h.update(chunk)
                after = item.stat()
                state = lambda s: [s.st_size, s.st_mtime_ns, s.st_ino, s.st_dev]
                if state(before) != state(after):
                    raise ValueError("Compiler file changed during observation")
                if outputs and (len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe"):
                    raise ValueError("Compiler output is not a Java class")
                inventory["files"][str(item.relative_to(root))] = {
                    "sha256": h.hexdigest(),
                    "bytes": after.st_size,
                    "stat": state(after),
                    **({"class_header": header.hex()} if outputs else {}),
                }
        return inventory

    for plan in plans:
        row = {"plan": plan, "errors": []}
        try:
            row["inputs"] = files(plan["inputs"], False)
            row["outputs"] = files(plan["outputs"], True)
        except (OSError, ValueError) as exc:
            row["errors"].append(str(exc))
        result[plan["consumer_requirement"]] = row
    return result


def capture_program():
    return inspect.getsource(capture)


def _content(inventory):
    return {
        "roots": inventory["roots"],
        "files": {
            path: {k: v for k, v in row.items() if k != "stat"}
            for path, row in inventory["files"].items()
        },
    }


def continuity(base, plan, producer, consumer):
    """Verify four byte-bound observations from this attempt, never cached flags."""
    captured = []
    refs = []
    for invocation in (producer, consumer):
        pair = invocation.get("compilation_evidence") or {}
        for boundary in ("before", "after"):
            ref = pair[boundary]
            raw = load_json(bound_file(base, ref))
            if (
                raw.get("run_id") != invocation["run_id"]
                or raw.get("invocation_id") != invocation["invocation_id"]
                or raw.get("boundary") != boundary
            ):
                raise ValueError("Compiler observation belongs to another invocation")
            row = raw["observations"][plan["consumer_requirement"]]
            if row["plan"] != plan or row["errors"]:
                raise ValueError("Compiler continuity scope or observation is incomplete")
            captured.append(row)
            refs.append(ref)
    first_before, first_after, next_before, next_after = captured
    outputs = first_after["outputs"]["files"]
    if (
        not outputs
        or any(not r.get("class_header", "").startswith("cafebabe") for r in outputs.values())
        or not any(path.endswith(".java") for path in first_after["inputs"]["files"])
        or any(not any(path.startswith(root + "/") for path in outputs) for root in plan["outputs"])
        or first_before["outputs"]["files"] == outputs
        or any(
            _content(first_after[k]) != _content(current[k])
            for k in ("inputs", "outputs")
            for current in (next_before, next_after)
        )
    ):
        raise ValueError(
            "Compiled output is absent, stale, or inputs/outputs changed between commands"
        )
    return {
        "producer_invocation_id": producer["invocation_id"],
        "plan_sha256": canonical_digest(plan),
        "evidence_refs": refs,
    }


def runtime_identity(base, invocation):
    """Compare actual launcher observations, beyond a matching major constraint."""
    runtime = invocation["runtime"]
    if invocation.get("recorder_version") == "native-tool-hooks-v1":
        from .native_hooks import launcher_runtime
        return launcher_runtime(base, invocation)
    if runtime.get("source") == "sag_host_authorized_receipt":
        receipt = load_json(bound_file(base, runtime["sag_dispatch_receipt"]))
        jdk = receipt["effective_jdk"]
        dispatch = jdk["provenance"]["dispatch_runtime"]
        return {
            "jdk": {k: dispatch.get(k) for k in ("executable", "major", "version", "vendor")},
            "toolchain": receipt["toolchain_fingerprint"],
        }
    probe = runtime["launcher_probe"]
    return {
        "executable": probe["executable"],
        "output": bound_file(base, probe).read_text().strip(),
    }
