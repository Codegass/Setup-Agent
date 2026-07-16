import pytest

from sag.agent.history_state import HistoryActionState, decode_history_action_state


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"invocation_status": "pending", "operation_outcome": "unknown"},
            HistoryActionState.PENDING,
        ),
        (
            {
                "invocation_status": "completed",
                "operation_outcome": "success",
                "succeeded": False,
                "success": False,
            },
            HistoryActionState.SUCCESS,
        ),
        (
            {
                "invocation_status": "completed",
                "operation_outcome": "failed",
                "succeeded": True,
                "success": True,
            },
            HistoryActionState.FAILED,
        ),
        ({"succeeded": True}, HistoryActionState.SUCCESS),
        ({"success": False}, HistoryActionState.FAILED),
    ],
)
def test_history_state_decoder_prefers_canonical_axes_and_supports_persisted_spellings(
    entry, expected
):
    assert decode_history_action_state(entry) is expected
