"""Phase 4.1 — global wall-clock cap for the whole run."""

from sag.agent.react_engine import wall_clock_exceeded
from sag.config.settings import Config


def test_within_cap_not_exceeded():
    assert wall_clock_exceeded(start_time=1000.0, cap_seconds=7200, now=1000.0 + 3600) is False


def test_beyond_cap_exceeded():
    assert wall_clock_exceeded(start_time=1000.0, cap_seconds=7200, now=1000.0 + 7201) is True


def test_exactly_at_cap_not_exceeded():
    assert wall_clock_exceeded(start_time=1000.0, cap_seconds=7200, now=1000.0 + 7200) is False


def test_cap_disabled_with_none_or_nonpositive():
    assert wall_clock_exceeded(start_time=0.0, cap_seconds=None, now=1e12) is False
    assert wall_clock_exceeded(start_time=0.0, cap_seconds=0, now=1e12) is False
    assert wall_clock_exceeded(start_time=0.0, cap_seconds=-5, now=1e12) is False


def test_config_defaults():
    config = Config()
    assert config.max_wall_clock_seconds == 7200
    assert config.dispatch_soft_timeout_seconds == 900
    assert config.dispatch_poll_interval_seconds == 15


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("SAG_MAX_WALL_CLOCK_SECONDS", "123")
    monkeypatch.setenv("SAG_DISPATCH_SOFT_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("SAG_DISPATCH_POLL_INTERVAL_SECONDS", "7")
    config = Config.from_env()
    assert config.max_wall_clock_seconds == 123
    assert config.dispatch_soft_timeout_seconds == 45
    assert config.dispatch_poll_interval_seconds == 7


def test_phase_floor_defaults():
    config = Config()
    floors = config.phase_min_floors
    assert set(floors) == {"analyze", "build", "test", "report"}  # provision is first; needs no floor
    assert floors["report"] >= 5, "report must always get its turn"
    assert floors["test"] >= 8


def test_effective_floor_clamps_for_small_runs():
    from sag.config.settings import effective_phase_floor
    assert effective_phase_floor(12, max_iterations=150) == 12
    assert effective_phase_floor(12, max_iterations=20) <= 3, "tiny runs scale floors down"
    assert effective_phase_floor(12, max_iterations=20) >= 1
