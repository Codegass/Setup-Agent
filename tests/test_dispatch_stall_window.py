"""Dispatch stall window (spec 2026-08-06): hold while it progresses,
hand off when it stalls. Time is always injected — never a real sleep."""

import pytest

from sag.config.settings import Config


def test_stall_window_config_defaults_to_600():
    assert Config().dispatch_stall_seconds == 600


def test_stall_window_config_reads_env(monkeypatch):
    monkeypatch.setenv("SAG_DISPATCH_STALL_SECONDS", "120")
    assert Config.from_env().dispatch_stall_seconds == 120
