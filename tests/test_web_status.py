from sag.web.status import StatusTone, normalize_status, status_tone


def test_normalize_status_keeps_known_values():
    assert normalize_status("BUILD SUCCESS") == "success"
    assert normalize_status("running") == "running"
    assert normalize_status("exited") == "exited"
    assert normalize_status(None) == "none"


def test_status_tone_matches_demo_semantics():
    assert status_tone("success") == StatusTone.GREEN
    assert status_tone("partial") == StatusTone.AMBER
    assert status_tone("running") == StatusTone.BLUE
    assert status_tone("failed") == StatusTone.RED
    assert status_tone("unknown") == StatusTone.NEUTRAL
