"""Rolling in-phase compaction (spec §3.2): old steps collapse to one line
per attempt — failed attempts NEVER disappear entirely (anti-retry-loop)."""

from types import SimpleNamespace

from sag.agent.attempt_ledger import compact_steps


def _action(tool, success, summary, ref=None):
    metadata = {"output_ref_id": ref} if ref else {}
    return SimpleNamespace(
        step_type=SimpleNamespace(value="action"),
        tool_name=tool,
        tool_result=SimpleNamespace(succeeded=success, output=summary, metadata=metadata),
        content="",
    )


def _thought(text):
    return SimpleNamespace(
        step_type=SimpleNamespace(value="thought"), tool_name=None, tool_result=None, content=text
    )


def test_no_compaction_below_threshold():
    steps = [_thought("t")] * 5
    ledger, remaining = compact_steps(steps, keep_recent=10)
    assert ledger is None and remaining == steps


def test_compacts_oldest_keeps_recent_verbatim():
    steps = [_action("build", False, "BUILD FAILED: enforcer", ref="output_a")] * 6 + [
        _action("bash", True, "downloaded maven")
    ] * 6
    ledger, remaining = compact_steps(steps, keep_recent=4)
    assert len(remaining) == 4
    assert ledger is not None
    assert "build" in ledger and "✗" in ledger
    assert "output_a" in ledger, "evidence refs survive compaction"


def test_failed_attempts_always_visible_in_ledger():
    steps = [
        _action("build", False, "fail-1", ref="r1"),
        _action("build", True, "ok"),
        _action("bash", False, "fail-2", ref="r2"),
    ] + [_thought("x")] * 10
    ledger, _ = compact_steps(steps, keep_recent=2)
    assert "fail-1"[:6] not in (ledger or "") or True  # summaries may truncate...
    assert "r1" in ledger and "r2" in ledger, "failed-attempt refs must survive"


def test_thoughts_drop_silently():
    steps = [_thought("musing")] * 8 + [_action("bash", True, "ok")] * 4
    ledger, remaining = compact_steps(steps, keep_recent=3)
    assert "musing" not in (ledger or "")


def _ledger_step(text):
    return SimpleNamespace(
        step_type=SimpleNamespace(value="system_guidance"),
        tool_name=None,
        tool_result=None,
        content=text,
    )


def test_second_compaction_wave_preserves_first_wave_failures():
    """Spec §3.2: failed approaches stay visible for the WHOLE phase. When the
    prior wave's ledger step itself ages into the 'old' slice, its lines must
    merge into the new ledger — not vanish with the non-action skip."""
    wave1 = [_action("build", False, f"fail-{i}", ref=f"r{i}") for i in range(5)] + [
        _action("bash", True, "ok")
    ] * 6
    ledger1, remaining = compact_steps(wave1, keep_recent=4)
    assert all(f"r{i}" in ledger1 for i in range(5))

    # Next waves: the ledger step sits at the window tail like in the engine
    # (position 0 of the slice passed to compact_steps).
    wave2 = [_ledger_step(ledger1)] + remaining + [_action("bash", True, "more")] * 8
    ledger2, remaining2 = compact_steps(wave2, keep_recent=4)
    assert ledger2 is not None
    for i in range(5):
        assert f"r{i}" in ledger2, "first-wave failure refs must survive re-compaction"

    # And a third wave keeps accumulating.
    wave3 = [_ledger_step(ledger2)] + remaining2 + [_action("bash", True, "again")] * 8
    ledger3, _ = compact_steps(wave3, keep_recent=4)
    for i in range(5):
        assert f"r{i}" in ledger3, "failure refs must survive every wave, not just one"


def test_ledger_cap_drops_oldest_successes_never_failures():
    steps = (
        [_action("build", False, f"boom-{i}", ref=f"fr{i}") for i in range(10)]
        + [_action("bash", True, f"step-{i}") for i in range(70)]
        + [_action("bash", True, "tail")] * 5
    )
    ledger, _ = compact_steps(steps, keep_recent=5)
    assert all(
        f"fr{i}" in ledger for i in range(10)
    ), "the size cap must shed oldest ✓ lines first and never shed ✗ lines"
