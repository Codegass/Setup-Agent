"""Whether a phase band finished, decided once for every surface that says so.

A band carries two different facts that are easy to mistake for one. Its
``termination`` is the TRANSITION the run took out of the phase — the ledger's
``expected_kind``, one of ``advance`` / ``repair`` / ``evidence_close`` /
``report`` / ``flow_close``. Its gates carry the grading the phase was given.
A phase graded ``failed`` still transitions out: on every archived run whose
seal calls a phase blocked, that phase's band closed with ``evidence_close``.

So "the run left this phase" and "this phase finished" are different
questions, and reading the transition alone answers the first while sounding
like the second. The turn stream has always asked the second one — it is what
makes it print ``✗ test blocked`` instead of a tick — and this module is that
rule, named, so the stream and the result card cannot come to disagree about
the same band.

Measured across the 97 archived runs that carry both a ledger and a seal (449
bands): no band closes without a gate, every band the seal calls ``aborted``
is a band that never closed at all, and the fourteen bands the seal calls
``blocked`` all closed with ``evidence_close`` on a ``failed`` grading.
"""

from __future__ import annotations

from typing import Any, Mapping

#: The one gate word that says a phase did not pass. The stream printed
#: `✗ <phase> blocked` off this word before this module existed.
BLOCKED_GATE_WORD = "failed"

#: The transition a phase takes when the run moves on to the next one. It is
#: the one close that is never read as blocked: the run advanced.
ADVANCE = "advance"

#: The transition that says the phase is being tried again. An attempt sent
#: back for repair is an attempt that did not finish.
REPAIR = "repair"


def blocked_close(termination: Any, gate_word: Any) -> bool:
    """Did this phase close on a grading that said it had not passed?

    `termination` is the transition kind, `gate_word` the last grading the
    band was given. Both are read as the ledger writes them.
    """

    return _word(termination) != ADVANCE and _word(gate_word) == BLOCKED_GATE_WORD


def band_finished(band: Any) -> bool:
    """Did the phase this band covers finish?

    Three ways it did not: the band never closed (the run stopped inside the
    phase), it closed for repair (the phase is being tried again), or it
    closed on a grading that said the phase had not passed.
    """

    termination = _word(_field(band, "termination"))
    if not termination or termination == REPAIR:
        return False
    return not blocked_close(termination, _last_gate_word(band))


def _last_gate_word(band: Any) -> str:
    """The grading the band ended on, or nothing when it was never graded."""

    gates = _field(band, "gates") or ()
    for gate in reversed(list(gates)):
        word = _word(_field(gate, "word"))
        if word:
            return word
    return ""


def _field(value: Any, name: str) -> Any:
    """One reader for a band that arrives as a model or as its own JSON."""

    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _word(value: Any) -> str:
    """The ledger's word, or an empty string — never `None` compared to a word."""

    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip()


__all__ = ["ADVANCE", "BLOCKED_GATE_WORD", "REPAIR", "band_finished", "blocked_close"]
