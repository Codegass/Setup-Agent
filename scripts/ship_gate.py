#!/usr/bin/env python3
"""Run every leg of the ship gate, and say what each one did.

The gate this replaces was one shell line::

    npm test --prefix webui && npx --prefix webui tsc -b && uv run pytest -q ...

It had two faults that hid each other. ``npx --prefix webui tsc -b`` does not
chdir into ``webui``: npx resolves the binary there and then TypeScript reads
``tsconfig.json`` from the *current* directory, so from the repo root it exits 1
with ``TS5083: Cannot read file '<repo>/tsconfig.json'`` no matter what the code
says. And because the legs were chained with ``&&``, that guaranteed failure
meant the Python suite never ran at all — a whole leg of the gate silently
absent for as long as nobody checked.

So: each leg runs, always, whatever the one before it did; each prints its own
verdict; and the exit code is non-zero if any leg failed.

The Python leg needs one more thing. This tree carries pre-existing failures
that belong to neither this plan nor its implementers, so "pytest exits 0" is
not a condition that can be met and asking for it invites someone to "fix"
tests that were never theirs. What the gate checks instead is the *set* of
failing test ids: it must equal ``PRE_EXISTING_FAILURES`` exactly. A new
failure fails the gate; a pre-existing failure that starts passing also fails
it, loudly, because that is a fact about the tree worth noticing and the list
below then needs editing.

Usage::

    uv run python scripts/ship_gate.py            # all four legs
    uv run python scripts/ship_gate.py --leg tsc  # one leg
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "webui"

#: Failing before phase 1 of this plan and still failing, verified at the
#: pre-implementation baseline commit ``7e3b0ede``. Not this plan's, not any
#: implementer's. ``test_bash_rg_reads_real_matches_with_context`` needs a real
#: ``rg`` binary on PATH and passes on a host that has one, so the gate accepts
#: its absence from the failing set as well as its presence.
PRE_EXISTING_FAILURES = {
    "tests/test_coverage_basis.py::test_the_unnarrowed_state_cannot_pair_a_passing_verdict_with_a_minority_scan",
    "tests/test_dispatch_and_poll.py::test_completed_build_primary_ref_reaches_unabridged_middle_via_search_and_shell[maven]",
    "tests/test_dispatch_and_poll.py::test_completed_build_primary_ref_reaches_unabridged_middle_via_search_and_shell[gradle]",
    "tests/test_python_phase_guidance.py::test_test_intro_carries_pytest_objective",
    "tests/test_python_phase_guidance.py::test_maven_build_intro_matches_facts_contract",
    "tests/test_python_phase_guidance.py::test_maven_test_intro_matches_facts_contract",
    "tests/test_python_phase_guidance.py::test_live_python_test_intro_carries_pytest_objective",
}

#: Fails only where ripgrep is not a real binary on PATH.
HOST_DEPENDENT_FAILURES = {
    "tests/test_tool_output_access.py::test_bash_rg_reads_real_matches_with_context",
}


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    print(f"\n$ (cd {cwd.relative_to(ROOT)} && {' '.join(argv)})", flush=True)
    finished = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})}
    )
    output = finished.stdout + finished.stderr
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return finished.returncode, output


def leg_vitest() -> tuple[bool, str]:
    code, output = _run(["npm", "test"], cwd=WEBUI)
    tail = next(
        (line.strip() for line in reversed(output.splitlines()) if "Tests " in line),
        "no test count found",
    )
    return code == 0, tail


def leg_tsc() -> tuple[bool, str]:
    # From `webui`, not `npx --prefix webui`: tsc reads tsconfig.json from the
    # working directory, and the repo root has none.
    code, output = _run(["npx", "tsc", "-b"], cwd=WEBUI)
    errors = len(re.findall(r"error TS\d+", output))
    return code == 0, "clean" if code == 0 else f"{errors} TypeScript errors"


def leg_build() -> tuple[bool, str]:
    code, output = _run(["npm", "run", "build"], cwd=WEBUI)
    built = next((line.strip() for line in output.splitlines() if "built in" in line), "")
    return code == 0, built or ("succeeded" if code == 0 else "failed")


def leg_pytest() -> tuple[bool, str]:
    _, output = _run(
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "-rf",
            "--ignore=tests/test_packaging_smoke.py",
        ],
        cwd=ROOT,
        env={"PYTHONPATH": f"{ROOT}:{ROOT / 'tests'}"},
    )
    failed = {
        line.split(" ", 1)[1].strip()
        for line in output.splitlines()
        if line.startswith("FAILED ")
    }
    new = sorted(failed - PRE_EXISTING_FAILURES - HOST_DEPENDENT_FAILURES)
    fixed = sorted(PRE_EXISTING_FAILURES - failed)
    if new:
        return False, f"{len(new)} NEW failure(s): " + ", ".join(new)
    if fixed:
        return False, (
            f"{len(fixed)} listed pre-existing failure(s) now pass — update "
            "PRE_EXISTING_FAILURES: " + ", ".join(fixed)
        )
    return True, f"{len(failed)} failing, all of them pre-existing"


LEGS = {
    "vitest": ("npm test --prefix webui", leg_vitest),
    "tsc": ("(cd webui && npx tsc -b)", leg_tsc),
    "build": ("npm run build --prefix webui", leg_build),
    "pytest": ("uv run pytest -q -rf --ignore=tests/test_packaging_smoke.py", leg_pytest),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leg", action="append", choices=sorted(LEGS), help="run only this leg")
    args = parser.parse_args()
    chosen = args.leg or list(LEGS)

    results: list[tuple[str, bool, str]] = []
    for name in chosen:
        _, run = LEGS[name]
        ok, note = run()
        results.append((name, ok, note))

    print("\n" + "=" * 72)
    for name, ok, note in results:
        command, _ = LEGS[name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<7} {command}\n        {note}")
    print("=" * 72)
    failed = [name for name, ok, _ in results if not ok]
    print(f"SHIP GATE {'FAILED: ' + ', '.join(failed) if failed else 'PASSED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
