import json

from sag.agent.context_journal import ContextJournal

JOURNAL_DIR = "/workspace/.setup_agent/contexts/journal"


class FakeOrchestrator:
    def __init__(self, fail=False):
        self.commands = []
        self.fail = fail

    def execute_command(self, command, **kwargs):
        if self.fail:
            raise RuntimeError("docker down")
        self.commands.append(command)
        return {"exit_code": 0, "output": ""}


def test_appends_one_line_per_iteration_in_container():
    orch = FakeOrchestrator()
    j = ContextJournal(orch)
    j.record(phase="build", iteration=7,
             segments={"goal_digest": 120, "ledger": 0, "history_entries": 14},
             delta={"added": 2, "compacted": 0}, total_chars=8000)

    appends = [c for c in orch.commands if f"{JOURNAL_DIR}/phase_build.journal.jsonl" in c]
    assert appends, orch.commands
    assert ">>" in appends[-1], "must APPEND, never truncate"
    payload = appends[-1]
    record = json.loads(payload[payload.index("{"):payload.rindex("}") + 1])
    assert record["iteration"] == 7
    assert record["delta"]["compacted"] == 0


def test_separate_files_per_phase():
    orch = FakeOrchestrator()
    j = ContextJournal(orch)
    j.record(phase="build", iteration=1, segments={}, delta={}, total_chars=10)
    j.record(phase="test", iteration=2, segments={}, delta={}, total_chars=10)
    joined = "\n".join(orch.commands)
    assert f"{JOURNAL_DIR}/phase_build.journal.jsonl" in joined
    assert f"{JOURNAL_DIR}/phase_test.journal.jsonl" in joined


def test_never_raises_on_orchestrator_error():
    j = ContextJournal(FakeOrchestrator(fail=True))
    j.record(phase="build", iteration=1, segments={}, delta={}, total_chars=1)  # must not raise


def test_window_texts_recorded_when_changed():
    orch = FakeOrchestrator()
    j = ContextJournal(orch)
    j.record(phase="build", iteration=1, segments={}, delta={}, total_chars=10,
             intro_text="=== PHASE: BUILD ===\nobjective...", ledger_text=None, step_span=1)
    j.record(phase="build", iteration=2, segments={}, delta={}, total_chars=12,
             intro_text=None, ledger_text="ATTEMPT LEDGER:\n✗ build: ...", step_span=5)

    appends = [c for c in orch.commands if "phase_build.journal.jsonl" in c]
    assert "PHASE: BUILD" in appends[0]
    assert "ATTEMPT LEDGER" in appends[1]
    import json as _json
    rec2 = _json.loads(appends[1][appends[1].index("{"):appends[1].rindex("}") + 1])
    assert rec2["step_span"] == 5
    assert rec2.get("intro_text") is None


# --- engine-side text gating (round-6 review) --------------------------------
# compact_steps returns the FULL cumulative ledger on every post-compaction
# iteration, so the ENGINE must gate ledger_text on text change — gating on
# n_compacted re-records ~6KB per iteration and stamps every `sag inspect`
# timeline row with [LEDGER].


def _engine_for_journal():
    from types import SimpleNamespace

    from sag.agent.phase_machine import PhaseMachine
    from sag.agent.react_engine import ReActEngine

    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine()  # current phase: provision
    engine.steps = [SimpleNamespace(content="=== PHASE: PROVISION ===")]
    engine.current_iteration = 31
    engine._journal_intro_dirty = False
    engine._journal_last_ledger = None
    orch = FakeOrchestrator()
    engine.context_journal = ContextJournal(orch)
    return engine, orch


def _journal_payloads(orch):
    return [
        json.loads(c[c.index("{"):c.rindex("}") + 1])
        for c in orch.commands
        if "journal.jsonl" in c
    ]


def test_engine_journals_ledger_text_only_when_it_changed():
    engine, orch = _engine_for_journal()
    ledger = "ATTEMPT LEDGER (older work, compacted):\n✗ build: boom"

    engine._record_context_journal(ledger, n_compacted=31, added=2, total_chars=100)
    engine.current_iteration += 1
    engine._record_context_journal(ledger, n_compacted=31, added=2, total_chars=110)
    engine.current_iteration += 1
    grown = ledger + "\n✗ build: boom again"
    engine._record_context_journal(grown, n_compacted=33, added=2, total_chars=120)

    recs = _journal_payloads(orch)
    assert len(recs) == 3
    assert recs[0].get("ledger_text") == ledger
    assert "ledger_text" not in recs[1], "unchanged ledger must not be re-recorded"
    assert recs[2].get("ledger_text") == grown
    # The segment SIZE keeps describing the window on every record.
    assert recs[1]["segments"]["ledger"] == len(ledger)


def test_engine_journals_nothing_for_ledgerless_iterations():
    engine, orch = _engine_for_journal()

    engine._record_context_journal(None, n_compacted=0, added=2, total_chars=50)

    recs = _journal_payloads(orch)
    assert len(recs) == 1
    assert "ledger_text" not in recs[0]
    assert recs[0]["segments"]["ledger"] == 0
