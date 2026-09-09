"""Checks of the fixed L1 probe's evidence and dispatch protocol."""

import hashlib

import pytest

from scripts.d3r2_l1_probes import fixtures, report_facts, steps


def test_l1_counts_only_hash_verified_xml_for_each_attempt(monkeypatch):
    red = '<testsuite><testcase name="ready"><failure/></testcase></testsuite>'
    green = '<testsuite><testcase name="ready"/></testsuite>'
    bodies = {"/reports/first.xml": red, "/reports/second.xml": green}
    monkeypatch.setattr(
        "sag.runtime.container_io.read_container_text",
        lambda audit, path, *, exact_bytes: bodies.get(path) if exact_bytes else None,
    )
    receipts = [
        {
            "receipt_id": f"r{i}",
            "contract_id": f"c{i}",
            "exit_code": int(i == 0),
            "actual_cwd": "/workspace/fixture",
            "argv": "python -m pytest tests/",
            "report_delta": {
                "new": [{"path": path, "sha256": hashlib.sha256(body.encode()).hexdigest()}],
                "changed": [],
            },
        }
        for i, (path, body) in enumerate(bodies.items())
    ]
    assert [(r["executed"], r["red"]) for r in report_facts(None, receipts)] == [(1, 1), (1, 0)]
    bodies["/reports/first.xml"] = green
    with pytest.raises(RuntimeError, match="XML hash"):
        report_facts(None, receipts)


def test_l1_runner_projection_preserves_make_setup_and_maven_producer_order():
    from sag.tools.build.backends import source_command_tokens

    for name in fixtures():
        for step in steps(name):
            params = step["params"]
            if step["tool"] == "build":
                assert source_command_tokens(
                    params["source_command"], params["system"], params["action"], params.get("args")
                )
    python = steps("make-pytest-repair")
    assert python[0]["params"] == python[2]["params"]
    assert python[1]["params"]["command"] == "make prepare"
    assert [s["params"]["action"] for s in steps("maven-parent-shade")] == ["install", "verify"]
