# tests/test_react_engine_progress_guard.py
from sag.agent.react_engine import NoProgressGuard


def test_guard_trips_after_threshold_without_any_artifacts():
    guard = NoProgressGuard(threshold=3)
    # No build artifact ever appears -> stuck after `threshold` completed tasks.
    assert guard.update(artifact_signal=0) is False
    assert guard.update(artifact_signal=0) is False
    assert guard.update(artifact_signal=0) is True


def test_guard_never_trips_once_artifacts_appear():
    guard = NoProgressGuard(threshold=3)
    assert guard.update(artifact_signal=0) is False
    assert guard.update(artifact_signal=5) is False  # built something -> progress
    # Later stagnation must NOT halt a run that already produced artifacts
    # (e.g. the test/report phase after a successful build).
    assert guard.update(artifact_signal=5) is False
    assert guard.update(artifact_signal=0) is False
    assert guard.update(artifact_signal=0) is False
