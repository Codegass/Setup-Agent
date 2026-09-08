# scripts/d3_reassemble_v2.py
"""Re-assemble every frozen d3 seat to a v2 target record, offline first.

Pins do not move: the v1 record and its digest stay as frozen.  This writes
`target_record.v2.json` beside it and an addendum manifest with the v2
digest and the matched cell's modules basis.  With --refetch it first asks
the courier for job logs (90-day cliff) and build definition files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.d3_harvest_target import HarvestError, fetch_logs, fetch_sources, harvest_from_dir

FREEZE = Path("logs/d3-freeze-20260830")
GENERATED = frozenset({"target_record.v2.json", "d3-pin-manifest-v2-addendum.json"})


def _verify_frozen_inputs(freeze: Path, manifest: dict) -> None:
    """Check frozen bytes and pins before fetching or publishing anything."""
    for folder in [freeze, *(freeze / seat for seat in manifest["seats"])]:
        if not folder.resolve().is_relative_to(freeze.resolve()):
            raise HarvestError(f"seat leaves the frozen snapshot: {folder}")
        for line in (folder / "SHA256SUMS").read_text().splitlines():
            if not line.strip():
                continue
            expected, relative = line.split(maxsplit=1)
            relative = relative.lstrip("*")
            if relative in GENERATED:
                continue
            path = (folder / relative).resolve()
            if not path.is_relative_to(folder.resolve()):
                raise HarvestError(f"checksum path leaves the snapshot: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise HarvestError(f"frozen checksum mismatch: {folder.name}/{relative}")
    for seat, pin in manifest["seats"].items():
        original = (freeze / seat / "target_record.json").read_bytes()
        if hashlib.sha256(original).hexdigest() != pin["target_sha256"]:
            raise HarvestError(f"{seat}: original record does not match its frozen digest")
        record = json.loads(original)
        if (record.get("schema_version"), record.get("repo"), record.get("sha")) != (
            1,
            pin["repo"],
            pin["sha"],
        ):
            raise HarvestError(f"{seat}: original record does not match its frozen revision")


def _update_checksums(freeze: Path) -> None:
    """Add new files without rewriting the frozen entries or their identities."""
    for folder in [freeze, *(p for p in freeze.iterdir() if p.is_dir())]:
        path = folder / "SHA256SUMS"
        previous = path.read_text() if path.exists() else ""
        previous_lines = []
        for line in previous.splitlines(keepends=True):
            pair = line.split(maxsplit=1)
            relative = pair[1].strip().lstrip("*") if len(pair) == 2 else ""
            if relative in GENERATED and (folder / relative).is_file():
                line = (
                    f"{hashlib.sha256((folder / relative).read_bytes()).hexdigest()}  {relative}\n"
                )
            previous_lines.append(line)
        updated = "".join(previous_lines)
        if updated != previous:
            path.write_text(updated)
            previous = updated
        existing = {
            line.split(maxsplit=1)[1].lstrip(" *")
            for line in previous.splitlines()
            if len(line.split(maxsplit=1)) == 2
        }
        additions = []
        files = folder.rglob("*") if folder != freeze else folder.iterdir()
        for artifact in sorted(files):
            if not artifact.is_file() or artifact.name == "SHA256SUMS":
                continue
            relative = artifact.relative_to(folder).as_posix()
            if relative not in existing:
                additions.append(
                    f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {relative}\n"
                )
        if additions:
            with path.open("a") as handle:
                if previous and not previous.endswith("\n"):
                    handle.write("\n")
                handle.writelines(additions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", type=Path, default=FREEZE)
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--jdk", type=int, default=17)
    args = parser.parse_args(argv)
    freeze = args.freeze
    try:
        manifest = json.loads((freeze / "d3-pin-manifest.json").read_text())
        _verify_frozen_inputs(freeze, manifest)
    except (HarvestError, OSError, ValueError, KeyError) as exc:
        print(f"Frozen input verification failed: {exc}", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    addendum = {
        "schema_version": 1,
        "reassembled_at": stamp,
        "record_schema_version": 2,
        "seats": {},
    }
    for seat, pin in sorted(manifest["seats"].items()):
        snapshot = freeze / seat
        original_path = snapshot / "target_record.json"
        original_record = json.loads(original_path.read_text())
        original_sha256 = pin["target_sha256"]
        notes: list[str] = []
        pending = snapshot / ".target_record.v2.pending.json"
        try:
            if args.refetch:
                notes.extend(fetch_logs(pin["repo"], snapshot))
                notes.extend(fetch_sources(pin["repo"], pin["sha"], snapshot))
            record, digest, _ = harvest_from_dir(
                snapshot,
                repo=pin["repo"],
                sha=pin["sha"],
                harvested_at=stamp,
                jdk_major=args.jdk,
                out_path=pending,
            )
            if record.matched_cell != original_record["matched_cell"]:
                raise HarvestError(f"{seat}: reassembly changed the frozen matched cell")
            pending.replace(snapshot / "target_record.v2.json")
        except (HarvestError, OSError, ValueError) as exc:
            pending.unlink(missing_ok=True)
            addendum["seats"][seat] = {"error": str(exc), "courier_notes": notes}
            print(f"{seat}: ERROR {exc}", file=sys.stderr)
            continue
        matched = next((c for c in record.cells if c.cell_id == record.matched_cell), None)
        addendum["seats"][seat] = {
            "repo": pin["repo"],
            "sha": pin["sha"],
            "record_v1_sha256": original_sha256,
            "record_v2_sha256": digest,
            "matched_cell": record.matched_cell,
            "matched_modules_basis": matched.modules_basis if matched else None,
            "matched_modules_count": len(matched.modules) if matched else 0,
            "matched_command": matched.command if matched else None,
            "cells_with_modules": sum(1 for c in record.cells if c.modules),
            "courier_notes": notes,
        }
        print(
            f"{seat}: v2 sha256={digest[:12]} basis={addendum['seats'][seat]['matched_modules_basis']} modules={addendum['seats'][seat]['matched_modules_count']}"
        )
    (freeze / "d3-pin-manifest-v2-addendum.json").write_text(
        json.dumps(addendum, indent=1, sort_keys=True) + "\n"
    )
    _update_checksums(freeze)
    return int(any("error" in value for value in addendum["seats"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
