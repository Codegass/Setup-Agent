"""The exported stdlib evaluator must not depend on the development checkout."""
import os
from pathlib import Path
import subprocess
import sys

from scripts.export_requirements_evaluator import export


def test_export_contains_all_native_evidence_dependencies(tmp_path):
    metadata, original, destination = (tmp_path / name for name in ("metadata", "original", "export"))
    metadata.mkdir()
    original.mkdir()
    for name in ("tasks", "historical-targets", "ci"):
        (original / name).mkdir()
    for path in (original / "manifest.json", metadata / "manifest.json"):
        path.write_text("{}")
    (metadata / "README.md").write_text("Synthetic export fixture")
    export(Path(__file__).resolve().parents[1], metadata, original, destination)
    script = "import sys;sys.path.insert(0,sys.argv[1]);from sag.benchmark import evaluator,recorder,jvm_inputs,compilation_evidence,ci_sources,ci_verification,native_tests;native_tests.validate({'kind':'test','validation':{'rule':'junit'}});print('portable imports valid')"
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    result = subprocess.run([sys.executable, "-I", "-c", script, str(destination)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "portable imports valid"
