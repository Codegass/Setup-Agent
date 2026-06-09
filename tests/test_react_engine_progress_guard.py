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


def test_guard_never_trips_when_artifacts_not_expected():
    # Non-Java projects (Node/Python/Rust/Go) never emit .class/JAR files, so
    # the artifact signal is structurally 0. A healthy run that completes far
    # more than `threshold` tasks must NOT be force-stopped.
    guard = NoProgressGuard(threshold=3)
    for _ in range(10):
        assert guard.update(artifact_signal=0, artifacts_expected=False) is False
    # And once artifacts ARE expected, the guard arms normally.
    assert guard.update(artifact_signal=0, artifacts_expected=True) is False
    assert guard.update(artifact_signal=0, artifacts_expected=True) is False
    assert guard.update(artifact_signal=0, artifacts_expected=True) is True
