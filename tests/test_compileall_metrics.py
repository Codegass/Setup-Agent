import compileall
import importlib.util
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


def test_command_quotes_interpreter_and_roots():
    command = compileall_metrics_command(
        "/workspace/proj/.venv/bin/python",
        ["/workspace/proj/src/pkg one", "/workspace/proj/src/pkg-two"],
    )

    assert command.startswith("/workspace/proj/.venv/bin/python -c ")
    assert "'/workspace/proj/src/pkg one'" in command
    assert "/workspace/proj/src/pkg-two" in command
