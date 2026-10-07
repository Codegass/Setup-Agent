"""Reproduce small, source-bound excerpts; never copy credentials or full logs.

Run from the repository root. The archived campaign is not needed to run tests.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path("output/requirements20-mini-20260923")
analysis = json.loads((ROOT / "token-investigation-20260923/request-analysis.json").read_text())
selections = {
    ("commons-dbcp", 29): ("report_tail", [18, 19, 20, 21, 22]),
    ("commons-dbutils", 15): ("sealed_reads", [2, 3, 4, 5, 6, 7]),
    ("commons-dbutils", 12): ("completed_tests", [3, 4, 5]),
}
fixtures = {}
for row in analysis["requests"]:
    selection = selections.get((row["project"], row["actor_iteration"]))
    if selection is None or row["harness"] != "sag" or row["role"] != "actor":
        continue
    name, indices = selection
    source = ROOT / "pilot" / row["request_ref"]["path"]
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["request_ref"]["sha256"]
    messages = json.loads(raw)["messages"]
    fixtures[name] = {
        "request_id": row["request_id"],
        "source": str(source),
        "source_sha256": row["request_ref"]["sha256"],
        "extraction": "Exact provider request messages at the listed zero-based indices.",
        "message_indices": indices,
        "messages": [messages[index] for index in indices],
    }
Path(__file__).with_name("archived_excerpts.json").write_text(
    json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n"
)
