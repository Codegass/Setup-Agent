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
