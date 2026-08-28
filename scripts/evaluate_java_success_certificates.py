#!/usr/bin/env python3
"""Evaluate checked-in Java success-certificate inputs.

The command is intentionally a pure projection over reviewed inputs.  It does
not scrape console logs or infer obligation identities from filenames.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sag.agent.java_success_certificates import (
    evaluate_java_success_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    rendered = (
        json.dumps(
            evaluate_java_success_manifest(payload),
            sort_keys=True,
            ensure_ascii=True,
            indent=2,
        )
        + "\n"
    )
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
