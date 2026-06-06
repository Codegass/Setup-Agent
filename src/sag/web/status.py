"""Status normalization for SAG web read models."""

from enum import StrEnum
from typing import Any


class StatusTone(StrEnum):
    NEUTRAL = "neutral"
    BLUE = "blue"
    GREEN = "green"
    RED = "red"
    AMBER = "amber"


_ALIASES = {
    "build success": "success",
    "build failure": "failure",
    "passed": "pass",
    "failed": "failed",
    "fail": "failed",
    "available": "available",
    "connected": "connected",
}


_TONES = {
    "success": StatusTone.GREEN,
    "pass": StatusTone.GREEN,
    "completed": StatusTone.GREEN,
    "ready": StatusTone.GREEN,
    "available": StatusTone.GREEN,
    "running": StatusTone.BLUE,
    "connected": StatusTone.BLUE,
    "active": StatusTone.BLUE,
    "partial": StatusTone.AMBER,
    "stopped": StatusTone.AMBER,
    "exited": StatusTone.RED,
    "failure": StatusTone.RED,
    "failed": StatusTone.RED,
    "blocked": StatusTone.RED,
}


def normalize_status(value: Any) -> str:
    if value is None:
        return "none"
    status = str(value).strip().lower().replace("_", " ")
    return _ALIASES.get(status, status.replace(" ", "-") if " " in status else status)


def status_tone(value: Any) -> StatusTone:
    return _TONES.get(normalize_status(value), StatusTone.NEUTRAL)
