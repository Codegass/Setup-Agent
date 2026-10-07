"""Select agent-owned evidence for the common scorer; never execute a task.

The controller calls this after the harness terminates. It does not infer
success from a final answer or silently fall back to post-agent execution.
"""

from pathlib import Path

from sag.benchmark.requirements import bound_file, evaluation_identity, load_json


def require_capture_support(arm, *, qualification=False):
    """Avoid spending a campaign budget before native capture is qualified."""
    if arm not in {"sag", "claude", "opencode"}:
        raise ValueError("Unknown harness")
    if arm != "sag" and not qualification:
        raise ValueError(
            "Native baseline execution capture is not yet qualified for agent-execution-v1; "
            "no container or model request has been started. Use --native-capture-qualification for bounded qualification only."
        )


def agent_evidence(directory, arm, task, spec, run_id):
    directory = Path(directory).resolve()
    if arm == "sag":
        sessions = list((directory / "sag-native/logs").glob("session_*"))
        if len(sessions) != 1:
            raise ValueError("Expected one SAG session for agent execution evidence")
        base = sessions[0]
        paths = list((base / "requirements-evidence").glob("*/run.json"))
        if len(paths) != 1:
            raise ValueError("SAG native requirements evidence is unavailable")
        run = load_json(paths[0])
        if run.get("producer") != "sag_authorized_receipt_adapter":
            raise ValueError("SAG evidence does not come from its authorized receipt adapter")
        pin = load_json(bound_file(base, run["run_pin"]))
        if pin.get("run_id") != run_id or pin.get("target_repo_sha") != task["sha"]:
            raise ValueError("SAG evidence pin belongs to another run")
    elif arm in {"claude", "opencode"}:
        base = directory / "agent-records"
        run = load_json(base / "run.json")
        if run.get("producer") != "native_tool_observer_v1":
            raise ValueError("Baseline agent execution needs a qualified native observer")
        if run.get("capture_policy") != "native-tool-hooks-v1" or run.get("capture_closed") is not True:
            raise ValueError("Native observer capture was not sealed")
        if run.get("capture_errors"):
            raise ValueError("Native observer capture has gaps")
    else:
        raise ValueError("Unknown harness")
    if (run.get("execution_origin") != "agent" or run.get("run_id") != run_id
            or run.get("repo") != task["repo"] or run.get("commit") != task["sha"]
            or run.get("evaluation_identity") != evaluation_identity(spec)):
        raise ValueError("Agent execution evidence identity or origin differs")
    # Check each byte binding before selecting a source. The physical evaluator
    # additionally validates commands, runtime, reports, artifacts and lineage.
    for ref in run.get("invocations", []):
        record = load_json(bound_file(base, ref))
        if record.get("execution_origin") != "agent":
            raise ValueError("Agent run contains a foreign execution origin")
    return base, run
