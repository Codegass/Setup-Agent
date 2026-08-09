import compileall
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sag.testcases.compileall_metrics import (
    COMPILEALL_METRICS_CONFLICT,
    COMPILEALL_METRICS_SCRIPT,
    compileall_metrics_command,
    parse_compileall_metrics,
)


def _scan(*roots: Path):
    completed = subprocess.run(
        [sys.executable, "-c", COMPILEALL_METRICS_SCRIPT, *map(str, roots)],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_compileall_metrics(completed.stdout)


def test_same_tag_pyc_is_deduped_by_source_path(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "alpha.py").write_text("VALUE = 1\n")
    (package / "beta.py").write_text("VALUE = 2\n")
    assert compileall.compile_dir(package, quiet=1)

    metric = _scan(package, package)

    assert metric.status == "valid"
    assert metric.source_count == 2
    assert metric.compiled_source_count == 2
    assert metric.coverage == pytest.approx(1.0)
    assert metric.foreign_pyc_count == 0
    assert metric.conflicts == ()
    assert len(metric.source_basis_sha256) == 64
    assert len(metric.pyc_basis_sha256) == 64
    assert metric.source_basis_entry_count == 2
    assert metric.pyc_basis_entry_count == 2

    source_entries = [
        {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted((package / "alpha.py", package / "beta.py"))
    ]
    expected_source_basis = hashlib.sha256(
        json.dumps(source_entries, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert metric.source_basis_sha256 == expected_source_basis


def test_source_or_pyc_byte_change_changes_the_bound_basis_digest(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    source = package / "alpha.py"
    source.write_text("VALUE = 1\n")
    assert compileall.compile_file(source, quiet=1)

    first = _scan(package)
    source.write_text("VALUE = 2\n")
    second = _scan(package)

    assert second.source_basis_sha256 != first.source_basis_sha256
    # The old pyc is still the exact bytecode basis the second observation saw.
    assert second.pyc_basis_sha256 == first.pyc_basis_sha256


def test_same_tag_pytest_pyc_is_auxiliary_not_foreign(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    source = package / "alpha.py"
    source.write_text("VALUE = 1\n")
    assert compileall.compile_file(source, quiet=1)
    canonical_pyc = Path(importlib.util.cache_from_source(str(source)))
    pytest_pyc = canonical_pyc.with_name(f"{canonical_pyc.stem}-pytest-9.1.1{canonical_pyc.suffix}")
    pytest_pyc.write_bytes(canonical_pyc.read_bytes())

    metric = _scan(package)

    assert metric.status == "valid"
    assert metric.source_count == 1
    assert metric.compiled_source_count == 1
    assert metric.coverage == pytest.approx(1.0)
    assert metric.foreign_pyc_count == 0
    assert metric.conflicts == ()


def test_missing_same_tag_pyc_reduces_coverage_without_changing_basis(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    alpha = package / "alpha.py"
    beta = package / "beta.py"
    alpha.write_text("VALUE = 1\n")
    beta.write_text("VALUE = 2\n")
    assert compileall.compile_file(alpha, quiet=1)

    metric = _scan(package)

    assert metric.status == "valid"
    assert metric.source_count == 2
    assert metric.compiled_source_count == 1
    assert metric.coverage == pytest.approx(0.5)
    assert metric.missing_source_count == 1


def test_foreign_pyc_makes_metric_invalid_instead_of_clamping_to_one(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    source = package / "alpha.py"
    source.write_text("VALUE = 1\n")
    assert compileall.compile_file(source, quiet=1)
    foreign = package / "__pycache__" / "alpha.cpython-999.pyc"
    foreign.write_bytes(b"foreign bytecode")

    metric = _scan(package)

    assert metric.status == "invalid"
    assert metric.source_count == 1
    assert metric.compiled_source_count == 1
    assert metric.coverage is None
    assert metric.foreign_pyc_count == 1
    assert metric.conflicts == (COMPILEALL_METRICS_CONFLICT,)


def test_source_and_pyc_exclusions_share_the_same_basis(tmp_path):
    package = tmp_path / "pkg"
    tests = package / "tests"
    docs = package / "docs"
    hidden = package / ".venv"
    tests.mkdir(parents=True)
    docs.mkdir()
    hidden.mkdir()
    (package / "kept.py").write_text("VALUE = 1\n")
    (tests / "test_ignored.py").write_text("def test_x(): pass\n")
    (docs / "conf.py").write_text("project = 'ignored'\n")
    (hidden / "vendored.py").write_text("VALUE = 3\n")
    assert compileall.compile_dir(package, quiet=1)

    metric = _scan(package)

    assert metric.status == "valid"
    assert metric.source_count == 1
    assert metric.compiled_source_count == 1
    assert metric.foreign_pyc_count == 0


def test_symlinked_source_outside_the_compile_root_never_enters_the_bound_basis(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (package / "escape.py").symlink_to(outside)

    metric = _scan(package)

    assert metric.status == "unavailable"
    assert metric.source_count == 0
    assert metric.source_basis_entry_count == 0


def test_command_quotes_interpreter_and_roots():
    command = compileall_metrics_command(
        "/workspace/proj/.venv/bin/python",
        ["/workspace/proj/src/pkg one", "/workspace/proj/src/pkg-two"],
    )

    assert command.startswith("/workspace/proj/.venv/bin/python -c ")
    assert "'/workspace/proj/src/pkg one'" in command
    assert "/workspace/proj/src/pkg-two" in command


def test_parser_rejects_an_unbound_or_malformed_basis():
    payload = {
        "status": "valid",
        "source_count": 1,
        "compiled_source_count": 1,
        "missing_source_count": 0,
        "foreign_pyc_count": 0,
        "coverage": 1.0,
        "cache_tag": "cpython-312",
        "conflicts": [],
        "missing_sources": [],
        "foreign_pycs": [],
        "source_basis_sha256": "not-a-digest",
        "pyc_basis_sha256": "b" * 64,
        "source_basis_entry_count": 1,
        "pyc_basis_entry_count": 1,
    }

    with pytest.raises(ValueError, match="basis digest"):
        parse_compileall_metrics(json.dumps(payload))
