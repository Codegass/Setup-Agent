# tests/test_phase_tool.py
"""phase(action: done|blocked|note) — the model's entire lifecycle surface."""

from types import SimpleNamespace

from sag.tools.phase_tool import PhaseTool


class GateRecorder:
    def __init__(self, ok=True, reason="", suggestions=None):
        self.calls = []
        self.result = {"ok": ok, "reason": reason, "suggestions": suggestions or []}

    def __call__(self, phase, validator, orchestrator, project_name):
        self.calls.append(phase)
        return self.result


def _tool(gate, phase="build"):
    machine = SimpleNamespace(current_phase=phase, is_complete=False)
    return PhaseTool(
        machine=machine, validator=None, orchestrator=None,
        project_name="demo", gate_fn=gate,
    )


def test_done_passes_gate_and_signals_engine():
    gate = GateRecorder(ok=True)
    tool = _tool(gate)

    result = tool.execute(action="done", key_results="compiled 115 classes", evidence=["output_x"])

    assert result.success is True
    assert result.metadata["phase_signal"] == "done"
    assert result.metadata["key_results"] == "compiled 115 classes"
    assert gate.calls == ["build"]


def test_done_rejected_by_gate_returns_options_no_signal():
    gate = GateRecorder(ok=False, reason="no artifacts", suggestions=["build(action='compile')"])
    tool = _tool(gate)

    result = tool.execute(action="done", key_results="done!", evidence=[])

    assert result.success is False
    assert "phase_signal" not in result.metadata
    assert result.verdict == "failed"
    assert any("compile" in s for s in result.suggestions)
    assert any("blocked" in s for s in result.suggestions), "escape valve must be advertised"


def test_blocked_always_accepted():
    gate = GateRecorder(ok=False, reason="would reject done")
    tool = _tool(gate)

    result = tool.execute(action="blocked", reason="develocity plugin unresolvable", evidence=["job:a"])

    assert result.success is True
    assert result.metadata["phase_signal"] == "blocked"
    assert result.metadata["reason"] == "develocity plugin unresolvable"
    assert gate.calls == [], "blocked is never gated"


def test_blocked_requires_reason():
    result = _tool(GateRecorder()).execute(action="blocked", reason="")
    assert result.success is False


def test_note_recorded_no_signal():
    result = _tool(GateRecorder()).execute(action="note", text="trying maven 3.9.9 next")
    assert result.success is True
    assert "phase_signal" not in result.metadata


def test_machine_complete_rejects_actions():
    machine = SimpleNamespace(current_phase=None, is_complete=True)
    tool = PhaseTool(machine=machine, validator=None, orchestrator=None,
                     project_name="demo", gate_fn=GateRecorder())
    result = tool.execute(action="done", key_results="x")
    assert result.success is False
