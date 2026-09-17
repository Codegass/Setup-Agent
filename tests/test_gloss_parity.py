"""The browser's copy of the reason glosses says what the Python copy says.

`webui/src/lib/ciGlosses.ts` is a hand-kept copy of
`src/sag/result_card/glosses.py`: the codes arrive with the payload and the
sentences are presentation, so the browser reads them locally rather than
asking the server for a static table. The TypeScript file's own header names
this test as what keeps the two in step — and until now the file it named did
not exist, so the two copies could drift apart unseen for as long as nobody
opened both.

A code glossed on one side and not the other is the defect that matters: the
terminal explains a failure the web tab shows as a bare code, and the two
surfaces disagree about the same run.
"""

from __future__ import annotations

import re
from pathlib import Path

from sag.result_card.glosses import REASON_GLOSS
from sag.result_card.rows import _CI_STATUS_WORD

MIRROR = Path(__file__).resolve().parents[1] / "webui" / "src" / "lib" / "ciGlosses.ts"

_ENTRY = re.compile(r'^  "(?P<code>[^"]+)": "(?P<text>(?:[^"\\]|\\.)*)",$', re.MULTILINE)


def _mirror() -> dict[str, str]:
    """The `REASON_GLOSS` object literal, read back as a dict."""

    source = MIRROR.read_text(encoding="utf-8")
    head, _, rest = source.partition("REASON_GLOSS: Record<string, string> = {")
    assert rest, f"{MIRROR} no longer declares REASON_GLOSS as an object literal"
    del head
    body, closed, _ = rest.partition("\n}\n")
    assert closed, f"{MIRROR}'s REASON_GLOSS literal is not closed"
    entries = {
        match.group("code"): match.group("text").replace('\\"', '"').replace("\\'", "'")
        for match in _ENTRY.finditer(body)
    }
    declared = body.count('": "')
    assert len(entries) == declared, (
        f"{MIRROR} declares {declared} sentences but only {len(entries)} parse; a "
        "line this test cannot read is a line it cannot compare"
    )
    return entries


def test_the_browser_glosses_the_same_codes_as_the_terminal():
    mirror = _mirror()
    missing = sorted(set(REASON_GLOSS) - set(mirror))
    extra = sorted(set(mirror) - set(REASON_GLOSS))
    assert not missing, (
        f"glossed in Python and not in the browser: {missing} — add them to "
        f"{MIRROR.name} or the web tab prints the bare code"
    )
    assert not extra, (
        f"glossed in the browser and not in Python: {extra} — add them to "
        "src/sag/result_card/glosses.py or the terminal prints the bare code"
    )


def test_the_two_copies_say_the_same_words():
    mirror = _mirror()
    differing = {
        code: (REASON_GLOSS[code], mirror[code])
        for code in sorted(set(REASON_GLOSS) & set(mirror))
        if REASON_GLOSS[code] != mirror[code]
    }
    assert not differing, (
        "these codes are explained differently on the two surfaces, so one run "
        f"reads two ways: {differing}"
    )


def _mirror_literal(name: str, declaration: str) -> dict[str, str]:
    """One `Record<string, string>` object literal, read back as a dict."""

    source = MIRROR.read_text(encoding="utf-8")
    _, _, rest = source.partition(declaration)
    assert rest, f"{MIRROR} no longer declares {name} as an object literal"
    body, closed, _ = rest.partition("\n}\n")
    assert closed, f"{MIRROR}'s {name} literal is not closed"
    return {
        match.group("code"): match.group("text").replace('\\"', '"').replace("\\'", "'")
        for match in _ENTRY.finditer(body)
    }


def test_the_browser_spells_a_ci_verdict_the_way_the_terminal_spells_it():
    """One verdict, one word, on all four surfaces.

    `OfficialCITab` used to spell the verdict itself, so the day the shared
    Python layer stopped saying `invalid` the web tab would have gone on saying
    it beside a result band that said `not compared` — the same run reading two
    ways on one screen.
    """

    mirror = _mirror_literal("CI_STATUS_WORD", "CI_STATUS_WORD: Record<string, string> = {")
    assert mirror == _CI_STATUS_WORD, (
        "the browser and the terminal spell a comparison verdict differently: "
        f"browser {mirror}, terminal {dict(_CI_STATUS_WORD)}"
    )


# ── the provenance sentence ──────────────────────────────────────────────────

BAND = Path(__file__).resolve().parents[1] / "webui" / "src" / "pages" / "detail" / "ResultBand.tsx"


def test_the_browser_states_a_reconstruction_in_the_same_words_as_the_terminal():
    """Three renderers, three copies of one sentence, and no shared constant.

    The terminal and the report are held together by
    `tests/test_snapshot_surface_agreement.py`; the browser is a fourth
    surface in another language, so its copy is compared here. A reader
    holding a report beside the page is the only other thing that would catch
    a drift, and by then the two have been disagreeing for a while.
    """

    from sag.console.result_block import _OLDER_RECORD
    from sag.result_card.markdown import _OLDER_RECORD as _WRITTEN_RECORD

    source = BAND.read_text(encoding="utf-8")

    assert _OLDER_RECORD == _WRITTEN_RECORD
    assert f'"{_OLDER_RECORD}"' in source, (
        f"{BAND} does not state the sentence the terminal and the report print "
        f"for a reconstructed result ({_OLDER_RECORD!r})"
    )
    assert 'verdictSource === "legacy"' in source, (
        f"{BAND} states the sentence but not the condition the other two surfaces "
        "use, so it could print it for a reading"
    )
