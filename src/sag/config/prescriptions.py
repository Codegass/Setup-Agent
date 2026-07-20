"""The prescription treatment mask (analyzer diet, Category 3 A/B panel).

Five NAMED booleans — one per ablation dimension of the panel spec's
field-level mapping — control every channel through which a PRESCRIPTION
(pre-hoc advice about what to do) reaches the agent:

  plan_pipeline            (a) generator + plan text + plan metadata + plan→todo
  recommendation_fields    (b) goal/rationale prose in trunk/metadata/TEXT/intro
  project_brief            (c) the brief artifact + projection + analyze ref
  objectives_wording       (d) "Recommended Build/Tests" objective wording
  python_prehoc_guidance   (e) the pre-hoc python/native-first guidance block

`SAG_PRESCRIPTIONS=off` closes all five (arm F); unset/`on` keeps today's
behavior (arm P). A per-dimension `SAG_PRESCRIPTION_<NAME>=on|off` overrides
the base — that is how stage-2 ablation masks are expressed. The resolved
flags ride the run pin's `feature_flags`, so every recorded run carries its
exact treatment mask.

Two hard properties (panel review):
* UNRECOGNIZED values raise — `SAG_PRESCRIPTIONS=offf` silently becoming
  arm P would archive a run into the wrong experimental arm.
* The mask is CACHED for the process lifetime on first read — the run pin
  snapshots the flags once at startup, and behavior drifting from the pin
  (env mutated mid-process) would break pin/behavior identity. Tests reset
  via `reset_prescription_flags_cache()`.

Deliberately NOT closed by any flag: the corrective-loop allowlist (island
checklist, loop redirect, native smoke steer — reactive, evidence-triggered),
`_recommended_workdir`, the installer ladder, and the manifest's mechanical
fields — shared machinery per the panel spec's classification rule.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

PRESCRIPTION_FLAG_NAMES = (
    "plan_pipeline",
    "recommendation_fields",
    "project_brief",
    "objectives_wording",
    "python_prehoc_guidance",
)

_cached_flags: Optional[Dict[str, bool]] = None


def _parse(raw, default: bool, source: str) -> bool:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("off", "0", "false", "no"):
        return False
    if value in ("on", "1", "true", "yes"):
        return True
    raise ValueError(
        f"unrecognized value {raw!r} for {source} — use on/off "
        "(a typo must never silently select an experimental arm)"
    )


def prescription_flags() -> Dict[str, bool]:
    """The resolved five-boolean treatment mask.

    Read from the environment ONCE per process and cached — the run pin's
    snapshot and the runtime behavior must be the same truth. Unrecognized
    values raise instead of defaulting.
    """
    global _cached_flags
    if _cached_flags is None:
        base = _parse(os.getenv("SAG_PRESCRIPTIONS"), True, "SAG_PRESCRIPTIONS")
        _cached_flags = {
            name: _parse(
                os.getenv(f"SAG_PRESCRIPTION_{name.upper()}"),
                base,
                f"SAG_PRESCRIPTION_{name.upper()}",
            )
            for name in PRESCRIPTION_FLAG_NAMES
        }
    return dict(_cached_flags)


def reset_prescription_flags_cache() -> None:
    """Test hook: drop the process cache so a changed env is re-read."""
    global _cached_flags
    _cached_flags = None


def feature_flags_for_mask(mask: Dict[str, bool]) -> Dict[str, bool]:
    """A treatment mask as run-pin feature_flags entries.

    PURE and the ONE naming source for the five pin keys — the agent's run
    pin (via prescription_feature_flags) and the collector's bit-by-bit
    verification both call THIS, so the two sides can never disagree on key
    names."""
    return {f"prescription_{name}": bool(mask[name]) for name in PRESCRIPTION_FLAG_NAMES}


def prescription_feature_flags() -> Dict[str, bool]:
    """The CURRENT process mask as run-pin feature_flags entries."""
    return feature_flags_for_mask(prescription_flags())


def parse_treatment_mask(spec: str) -> Dict[str, bool]:
    """A collector-facing mask spec: 'on' (arm P), 'off' (arm F), or five
    0/1 characters in PRESCRIPTION_FLAG_NAMES order (stage-2 masks).
    Raises on anything else."""
    value = str(spec).strip().lower()
    if value == "on":
        return {name: True for name in PRESCRIPTION_FLAG_NAMES}
    if value == "off":
        return {name: False for name in PRESCRIPTION_FLAG_NAMES}
    if len(value) == len(PRESCRIPTION_FLAG_NAMES) and set(value) <= {"0", "1"}:
        return {name: bit == "1" for name, bit in zip(PRESCRIPTION_FLAG_NAMES, value)}
    raise ValueError(
        f"unrecognized treatment mask {spec!r} — use on, off, or "
        f"{len(PRESCRIPTION_FLAG_NAMES)} chars of 0/1 in "
        f"{'/'.join(PRESCRIPTION_FLAG_NAMES)} order"
    )


def treatment_mask_environment(mask: Dict[str, bool]) -> Dict[str, str]:
    """The env vars a runner must inject so a child SAG process resolves
    exactly `mask` — every dimension set explicitly, no inherited defaults."""
    return {
        "SAG_PRESCRIPTIONS": "on" if all(mask.values()) else "off",
        **{
            f"SAG_PRESCRIPTION_{name.upper()}": "on" if mask[name] else "off"
            for name in PRESCRIPTION_FLAG_NAMES
        },
    }
