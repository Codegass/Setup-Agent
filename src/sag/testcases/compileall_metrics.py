"""Honest, source-keyed compileall coverage shared by producers and validators."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Iterable, Literal

COMPILEALL_METRICS_CONFLICT = "metrics_conflict"
COMPILEALL_METRICS_UNAVAILABLE_CONFLICT = "compileall_metrics_unavailable"

# This script runs inside the target environment with the SAME interpreter
# used by compileall. That makes importlib.util.cache_from_source authoritative
# for the active cache tag without requiring SAG to be installed in the target.
COMPILEALL_METRICS_SCRIPT = """\
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

EXCLUDED_DIR_NAMES = {"tests", "docs", "examples"}

def excluded(relative_path):
    return any(
        part.startswith(".") or part in EXCLUDED_DIR_NAMES
        for part in relative_path.parts
    )

roots = []
seen_roots = set()
for raw_root in sys.argv[1:]:
    root = Path(raw_root).resolve()
    if root in seen_roots or not root.exists():
        continue
    seen_roots.add(root)
    roots.append(root)

def contained(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

sources = set()
pycs = set()
for root in roots:
    if root.is_file():
        if root.suffix == ".py":
            sources.add(root)
        elif root.suffix == ".pyc":
            pycs.add(root)
        continue
    for directory, dirnames, filenames in os.walk(root):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        dirnames[:] = [
            name
            for name in dirnames
            if not excluded(relative_directory / name)
        ]
        if excluded(relative_directory):
            continue
        for filename in filenames:
            candidate = directory_path / filename
            relative_path = candidate.relative_to(root)
            if excluded(relative_path):
                continue
            path = candidate.resolve()
            if not contained(path, root):
                continue
            if filename.endswith(".py"):
                sources.add(path)
            elif filename.endswith(".pyc"):
                pycs.add(path)

expected = {
    Path(importlib.util.cache_from_source(str(source))).resolve(): source
    for source in sources
}
cache_tag = sys.implementation.cache_tag or ""

def source_for_same_tag_pyc(path):
    if not cache_tag or path.parent.name != "__pycache__":
        return None
    marker = f".{cache_tag}"
    marker_index = path.name.rfind(marker)
    if marker_index <= 0 or not path.name.endswith(".pyc"):
        return None
    candidate = (path.parent.parent / f"{path.name[:marker_index]}.py").resolve()
    return candidate if candidate in sources else None

mapped_pycs = {}
for pyc in pycs:
    source = expected.get(pyc)
    if source is None:
        source = source_for_same_tag_pyc(pyc)
    if source is not None:
        mapped_pycs[pyc] = source

compiled_pyc_pairs = {
    expected_pyc: source
    for expected_pyc, source in expected.items()
    if expected_pyc.is_file()
}
compiled_pyc_pairs.update(mapped_pycs)
compiled_sources = set(compiled_pyc_pairs.values())
foreign_pycs = sorted(pycs - set(mapped_pycs))
missing_sources = sorted(sources - compiled_sources)
source_count = len(sources)
compiled_source_count = len(compiled_sources)

def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def basis_sha256(entries):
    body = json.dumps(entries, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(body).hexdigest()

source_basis = [
    {"path": str(path), "sha256": file_sha256(path)}
    for path in sorted(sources)
]
pyc_basis = [
    {
        "path": str(path),
        "sha256": file_sha256(path),
        "source": str(compiled_pyc_pairs[path]),
    }
    for path in sorted(compiled_pyc_pairs)
]

if foreign_pycs:
    status = "invalid"
    coverage = None
    conflicts = ["metrics_conflict"]
elif source_count == 0:
    status = "unavailable"
    coverage = None
    conflicts = []
else:
    status = "valid"
    coverage = compiled_source_count / source_count
    conflicts = []

payload = {
    "status": status,
    "source_count": source_count,
    "compiled_source_count": compiled_source_count,
    "missing_source_count": len(missing_sources),
    "foreign_pyc_count": len(foreign_pycs),
    "coverage": coverage,
    "cache_tag": cache_tag,
    "source_basis_sha256": basis_sha256(source_basis),
    "pyc_basis_sha256": basis_sha256(pyc_basis),
    "source_basis_entry_count": len(source_basis),
    "pyc_basis_entry_count": len(pyc_basis),
    "conflicts": conflicts,
    "missing_sources": [str(path) for path in missing_sources[:20]],
    "foreign_pycs": [str(path) for path in foreign_pycs[:20]],
}
print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
"""


@dataclass(frozen=True, slots=True)
class CompileallMetrics:
    status: Literal["valid", "invalid", "unavailable"]
    source_count: int
    compiled_source_count: int
    missing_source_count: int
    foreign_pyc_count: int
    coverage: float | None
    cache_tag: str
    source_basis_sha256: str
    pyc_basis_sha256: str
    source_basis_entry_count: int
    pyc_basis_entry_count: int
    conflicts: tuple[str, ...] = ()
    missing_sources: tuple[str, ...] = ()
    foreign_pycs: tuple[str, ...] = ()


def compileall_metrics_command(python: str, roots: Iterable[str]) -> str:
    arguments = " ".join(shlex.quote(str(root)) for root in roots)
    command = f"{shlex.quote(python)} -c {shlex.quote(COMPILEALL_METRICS_SCRIPT)}"
    return f"{command} {arguments}" if arguments else command


def parse_compileall_metrics(output: str) -> CompileallMetrics:
    text = (output or "").strip()
    if not text:
        raise ValueError("compileall metrics output is empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("\n{")
        candidate = text[start + 1 :] if start >= 0 else ""
        payload = json.loads(candidate)
    if not isinstance(payload, dict):
        raise ValueError("compileall metrics output must be a JSON object")

    status = payload.get("status")
    if status not in {"valid", "invalid", "unavailable"}:
        raise ValueError(f"invalid compileall metric status: {status!r}")
    integer_fields = (
        "source_count",
        "compiled_source_count",
        "missing_source_count",
        "foreign_pyc_count",
    )
    if any(type(payload.get(field)) is not int for field in integer_fields):
        raise ValueError("compileall metric counts must be integers")
    if any(payload[field] < 0 for field in integer_fields):
        raise ValueError("compileall metric counts must be non-negative")
    source_count = payload["source_count"]
    compiled_source_count = payload["compiled_source_count"]
    missing_source_count = payload["missing_source_count"]
    foreign_pyc_count = payload["foreign_pyc_count"]
    if compiled_source_count > source_count:
        raise ValueError("compiled source count exceeds source count")
    if missing_source_count != source_count - compiled_source_count:
        raise ValueError("missing source count disagrees with source mapping")

    coverage = payload.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, (int, float)) or isinstance(coverage, bool):
            raise ValueError("compileall coverage must be numeric or null")
        coverage = float(coverage)
        if not 0.0 <= coverage <= 1.0:
            raise ValueError("compileall coverage is outside [0, 1]")
    if status != "valid" and coverage is not None:
        raise ValueError("invalid or unavailable compileall metrics cannot have coverage")
    if status == "valid":
        expected_coverage = compiled_source_count / source_count if source_count else None
        if expected_coverage is None or coverage is None:
            raise ValueError("valid compileall metrics require a non-empty coverage basis")
        if abs(coverage - expected_coverage) > 1e-12:
            raise ValueError("compileall coverage disagrees with source mapping")
        if foreign_pyc_count:
            raise ValueError("valid compileall metrics cannot contain foreign pyc")
    if status == "invalid" and foreign_pyc_count <= 0:
        raise ValueError("invalid compileall metrics require a foreign pyc mismatch")
    if status == "unavailable" and source_count:
        raise ValueError("unavailable compileall metrics cannot hide source files")

    basis_digests = (
        payload.get("source_basis_sha256"),
        payload.get("pyc_basis_sha256"),
    )
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        for digest in basis_digests
    ):
        raise ValueError("compileall basis digest must be a SHA-256")
    basis_counts = (
        payload.get("source_basis_entry_count"),
        payload.get("pyc_basis_entry_count"),
    )
    if any(type(count) is not int or count < 0 for count in basis_counts):
        raise ValueError("compileall basis entry counts must be non-negative integers")
    if basis_counts[0] != source_count or basis_counts[1] < compiled_source_count:
        raise ValueError("compileall basis entry counts disagree with source mapping")

    conflicts = tuple(str(item) for item in payload.get("conflicts") or ())
    if status == "invalid" and COMPILEALL_METRICS_CONFLICT not in conflicts:
        raise ValueError("invalid compileall metrics require metrics_conflict")

    return CompileallMetrics(
        status=status,
        source_count=payload["source_count"],
        compiled_source_count=payload["compiled_source_count"],
        missing_source_count=payload["missing_source_count"],
        foreign_pyc_count=payload["foreign_pyc_count"],
        coverage=coverage,
        cache_tag=str(payload.get("cache_tag") or ""),
        source_basis_sha256=basis_digests[0],
        pyc_basis_sha256=basis_digests[1],
        source_basis_entry_count=basis_counts[0],
        pyc_basis_entry_count=basis_counts[1],
        conflicts=conflicts,
        missing_sources=tuple(str(item) for item in payload.get("missing_sources") or ()),
        foreign_pycs=tuple(str(item) for item in payload.get("foreign_pycs") or ()),
    )


__all__ = [
    "COMPILEALL_METRICS_CONFLICT",
    "COMPILEALL_METRICS_UNAVAILABLE_CONFLICT",
    "COMPILEALL_METRICS_SCRIPT",
    "CompileallMetrics",
    "compileall_metrics_command",
    "parse_compileall_metrics",
]
