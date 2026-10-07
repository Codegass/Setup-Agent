"""Reproducible offline requirement evaluation for any agent recorder."""

import argparse
import json
from pathlib import Path

from .evaluator import evaluate
from .requirements import aggregate, load_json, load_requirements, requirements_evidence_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("score")
    score.add_argument("--task", type=Path, required=True)
    score.add_argument("--requirements", type=Path, required=True)
    score.add_argument("--run", type=Path, required=True)
    score.add_argument(
        "--evidence-root",
        type=Path,
        help="Explicit trusted root for refs; SAG exports use the host session directory",
    )
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--ci-source-root", type=Path,
                       help="Frozen requirements/CI source archive; defaults to the supplied requirements location")
    score.add_argument("--required-execution-origin", choices=["agent", "independent_replay"],
                       help="Require agent-owned evidence or separately evaluate an independent replay; omission preserves historical evaluation")
    summary = commands.add_parser("aggregate")
    summary.add_argument("--requirements-dir", type=Path, required=True)
    summary.add_argument("--scores", type=Path, nargs="*", default=[])
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "score":
        task = load_json(args.task)
        spec = load_requirements(args.requirements, task)
        run = load_json(args.run)
        source_root = args.ci_source_root
        if source_root is None and "requirements_sources" not in run:
            source_root = requirements_evidence_root(args.requirements)
        result = evaluate(task, spec, run, args.evidence_root or args.run.parent,
                          ci_source_base=source_root, required_execution_origin=args.required_execution_origin)
    else:
        specs = [load_requirements(p) for p in sorted(args.requirements_dir.glob("*.json"))]
        if not specs or len({s["project_id"] for s in specs}) != len(specs):
            raise ValueError("Frozen cohort must contain unique projects")
        result = aggregate({s["project_id"]: s for s in specs}, [load_json(p) for p in args.scores])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
