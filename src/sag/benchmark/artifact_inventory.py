"""Read-only, descriptive JVM output inventories shared by every harness.

Counts are observations, not task verdicts. Only declared modules' conventional
output roots and explicitly declared artifact paths are inspected; dependencies
in a user's Maven cache and bytecode inside JARs are not class-file counts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time
import zipfile


def inventory_plan(spec, step_id=None):
    modules = {}
    missing_scope = []
    for step in spec["steps"]:
        if step_id is not None and step["step_id"] != step_id:
            continue
        if "modules" not in step:
            missing_scope.append(step["step_id"])
        for module in step.get("modules", []):
            key = (module["id"], module["path"])
            modules[key] = {"id": key[0], "path": key[1]}
    artifacts = {}
    for row in spec["requirements"]:
        if step_id is not None and row["step_id"] != step_id:
            continue
        for item in row.get("expectations", {}).get("artifacts", []):
            path = item.get("path")
            if isinstance(path, str) and not PurePosixPath(path).is_absolute():
                artifacts.setdefault(path, []).append({
                    k: item[k] for k in ("role", "classifier", "extension") if k in item
                })
    plan = {"modules": sorted(modules.values(), key=lambda r: (r["path"], r["id"])),
            "declared_artifacts": artifacts}
    if missing_scope:
        plan["unavailable_reason"] = "Module inventory scope is not declared for: " + ", ".join(missing_scope)
    return plan


def _relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError("Output inventory scope escapes reviewed checkout")
    return path


def _class_role(path):
    parts = PurePosixPath(path).parts
    if parts[:2] == ("target", "classes"):
        return "main"
    if parts[:2] == ("target", "test-classes"):
        return "test"
    if len(parts) >= 4 and parts[:2] == ("build", "classes"):
        if parts[3] in {"main", "test"}:
            return parts[3]
    return "unclassified"


def _jar_role(path, declared):
    roles = {r.get("role") for r in declared.get(path, [])}
    classifiers = {r.get("classifier") for r in declared.get(path, [])}
    aliases = {"main_archive": "main", "jpms_augmented_archive": "main",
               "source_archive": "sources", "test_archive": "tests",
               "test_source_archive": "test_sources"}
    roles = {aliases.get(role, role) for role in roles}
    if "test_sources" in roles or "test-sources" in classifiers:
        return "test_sources"
    for role in ("sources", "javadoc", "tests"):
        if role in roles or role in classifiers:
            return role
    if "main" in roles:
        return "main"
    return "unclassified"


def capture(root, plan, *, run_id, boundary, invocation_id=None):
    root = Path(root).absolute()
    started = time.monotonic()
    observed = {"schema_version": 1, "run_id": run_id, "invocation_id": invocation_id,
                "boundary": boundary, "observed_at": datetime.now(timezone.utc).isoformat(),
                "scope": plan, "files": [], "errors": [], "roots": {},
                "semantics": "Filesystem observations only; freshness and task success require bound before/after and separate acceptance."}
    if plan.get("unavailable_reason"):
        observed.update(status="unavailable", counts=None, seconds=time.monotonic()-started)
        observed["errors"].append({"path": ".", "reason": plan["unavailable_reason"]})
        return observed
    if not root.is_dir() or root.is_symlink():
        observed.update(status="unavailable", counts=None, seconds=time.monotonic()-started)
        observed["errors"].append({"path": ".", "reason": "Checkout directory unavailable or symlinked"})
        return observed

    def safe(path):
        if not path.is_relative_to(root):
            raise ValueError("Output inventory path escapes checkout")
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise ValueError("Symlink in output inventory: " + str(path.relative_to(root)))
            if parent == root:
                break
        return path

    paths = set()
    for module in plan["modules"]:
        module_path = _relative(module["path"])
        for output in ("target", "build"):
            relative = str(module_path / output)
            path = root / relative
            try:
                safe(path)
                observed["roots"][relative] = "directory" if path.is_dir() else "absent"
                if not path.exists():
                    continue
                def onerror(error): raise error
                for current, dirs, names in os.walk(path, followlinks=False, onerror=onerror):
                    for name in dirs:
                        safe(Path(current) / name)
                    for name in names:
                        item = Path(current) / name
                        if item.suffix.lower() in {".class", ".jar", ".war"}:
                            paths.add(safe(item))
            except (OSError, ValueError) as exc:
                observed["errors"].append({"path": relative, "reason": str(exc)})
    for relative in plan["declared_artifacts"]:
        try:
            path = safe(root / str(_relative(relative)))
            if path.exists() and path.suffix.lower() in {".jar", ".war", ".class"}:
                paths.add(path)
        except (OSError, ValueError) as exc:
            observed["errors"].append({"path": relative, "reason": str(exc)})
    for path in sorted(paths):
        relative = str(path.relative_to(root))
        try:
            before = safe(path).stat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Not a regular output file")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                header = stream.read(8); digest.update(header)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
            fingerprint = lambda s: [s.st_size, s.st_mtime_ns, s.st_ino, s.st_dev]
            if fingerprint(before) != fingerprint(after):
                raise ValueError("File changed during output inventory")
            matches = [m for m in plan["modules"] if Path(relative).is_relative_to(Path(m["path"]))]
            module = max(matches, key=lambda m: len(PurePosixPath(m["path"]).parts)) if matches else None
            module_relative = str(Path(relative).relative_to(Path(module["path"]))) if module else relative
            kind = path.suffix.lower()[1:]
            valid = len(header) == 8 and header[:4] == b"\xca\xfe\xba\xbe" if kind == "class" else zipfile.is_zipfile(path)
            if fingerprint(path.stat()) != fingerprint(after):
                raise ValueError("File changed during format inspection")
            observed["files"].append({"path": relative, "module": module["id"] if module else None,
                "module_path": module["path"] if module else None, "kind": kind,
                "role": _class_role(module_relative) if kind == "class" else _jar_role(relative, plan["declared_artifacts"]),
                "format_valid": valid, "sha256": digest.hexdigest(), "bytes": after.st_size,
                "fingerprint": fingerprint(after), **({"class_header": header.hex()} if kind == "class" else {})})
        except (OSError, ValueError) as exc:
            observed["errors"].append({"path": relative, "reason": str(exc)})
    observed["status"] = "complete" if not observed["errors"] else "partial"
    observed["counts"] = dict(sorted(Counter(
        r["kind"] + ":" + (r["role"] if r["format_valid"] else "invalid") for r in observed["files"]
    ).items()))
    observed["seconds"] = time.monotonic() - started
    return observed


def changed_outputs(before, after):
    """Same invocation only; an unchanged inherited file is not freshly built."""
    if any(before.get(k) != after.get(k) for k in ("run_id", "invocation_id", "scope")):
        raise ValueError("Output inventories have different producer scopes")
    if before["status"] != "complete" or after["status"] != "complete":
        return None
    old = {row["path"]: row for row in before["files"]}
    return [row for row in after["files"] if row != old.get(row["path"])]


def main():
    p = argparse.ArgumentParser(); p.add_argument("--requirements", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--boundary", required=True); p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    spec = json.loads(a.requirements.read_text())
    result = capture(a.root, inventory_plan(spec), run_id=a.run_id, boundary=a.boundary)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
