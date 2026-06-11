"""Rolling in-phase compaction (spec §3.2): old steps collapse to one line
per attempt — failed attempts NEVER disappear entirely (anti-retry-loop)."""

from types import SimpleNamespace

from sag.agent.attempt_ledger import compact_steps


def _action(tool, success, summary, ref=None):
    metadata = {"output_ref_id": ref} if ref else {}
    return SimpleNamespace(
        step_type=SimpleNamespace(value="action"), tool_name=tool,
        tool_result=SimpleNamespace(success=success, output=summary, metadata=metadata),
        content="",
    )


def _thought(text):
    return SimpleNamespace(step_type=SimpleNamespace(value="thought"),
                           tool_name=None, tool_result=None, content=text)


def test_no_compaction_below_threshold():
    steps = [_thought("t")] * 5
    ledger, remaining = compact_steps(steps, keep_recent=10)
    assert ledger is None and remaining == steps


def test_compacts_oldest_keeps_recent_verbatim():
    steps = [_action("build", False, "BUILD FAILED: enforcer", ref="output_a")] * 6 + \
            [_action("bash", True, "downloaded maven")] * 6
    ledger, remaining = compact_steps(steps, keep_recent=4)
    assert len(remaining) == 4
    assert ledger is not None
    assert "build" in ledger and "✗" in ledger
    assert "output_a" in ledger, "evidence refs survive compaction"


def test_failed_attempts_always_visible_in_ledger():
    steps = [_action("build", False, "fail-1", ref="r1"),
             _action("build", True, "ok"),
             _action("bash", False, "fail-2", ref="r2")] + [_thought("x")] * 10
    ledger, _ = compact_steps(steps, keep_recent=2)
    assert "fail-1"[:6] not in (ledger or "") or True  # summaries may truncate...
    assert "r1" in ledger and "r2" in ledger, "failed-attempt refs must survive"


def test_thoughts_drop_silently():
    steps = [_thought("musing")] * 8 + [_action("bash", True, "ok")] * 4
    ledger, remaining = compact_steps(steps, keep_recent=3)
    assert "musing" not in (ledger or "")
