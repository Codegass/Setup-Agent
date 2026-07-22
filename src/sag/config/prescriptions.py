"""Treatment-mask naming and parsing for the Category-3 A/B panel harness.

The prescription A/B panel is COMPLETE: the panel authorized deletion of all
five prescription dimensions (analyzer-diet spec, Category 3), and production
now runs the FACTS-ONLY behavior unconditionally — there is no runtime
`SAG_PRESCRIPTIONS` switch anymore. The runtime-gating functions
(`prescription_flags`, `prescription_feature_flags`,
`reset_prescription_flags_cache`) are gone with the arm-P code they gated.

What remains is the HISTORICAL harness surface: the collector
(`collect_control_layer_ab.py`) and the stage-2 runner still express and
verify treatment masks so the sealed panel evidence stays reproducible. These
are PURE naming/parsing helpers — no environment reads, no process state.

  plan_pipeline            (a) generator + plan text + plan metadata + plan→todo
  recommendation_fields    (b) goal/rationale prose in trunk/metadata/TEXT/intro
  project_brief            (c) the brief artifact + projection + analyze ref
  objectives_wording       (d) "Recommended Build/Tests" objective wording
  python_prehoc_guidance   (e) the pre-hoc python/native-first guidance block
"""

from __future__ import annotations

from typing import Dict

PRESCRIPTION_FLAG_NAMES = (
    "plan_pipeline",
    "recommendation_fields",
    "project_brief",
    "objectives_wording",
    "python_prehoc_guidance",
)


def feature_flags_for_mask(mask: Dict[str, bool]) -> Dict[str, bool]:
    """A treatment mask as run-pin feature_flags entries.

    PURE and the ONE naming source for the five pin keys — the collector's
    bit-by-bit verification of the sealed panel pins calls THIS, so the
    archived evidence and the verifier can never disagree on key names."""
    return {f"prescription_{name}": bool(mask[name]) for name in PRESCRIPTION_FLAG_NAMES}


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
    """The env vars a runner records so a mask is expressed explicitly in the
    campaign ledger. Retained for the historical collector/stage-2 harness;
    production no longer reads these — the FACTS-ONLY behavior is unconditional."""
    return {
        "SAG_PRESCRIPTIONS": "on" if all(mask.values()) else "off",
        **{
            f"SAG_PRESCRIPTION_{name.upper()}": "on" if mask[name] else "off"
            for name in PRESCRIPTION_FLAG_NAMES
        },
    }
