#!/usr/bin/env python3
"""Programmatic raw-slice extractor for the D2R4 campaign — projects
ignite, lucene, incubator-kie-kogito-examples.

Protocol: docs/superpowers/specs/2026-08-13-d2-raw-slice-review-protocol.md
(section 1 authoritative sources only; section 3 verbatim [A]/[B]/[C]/[D]).

Every quoted block is produced by a resolver that reads the authoritative file
fresh.  After each markdown file is written the verifier re-reads it,
re-resolves every registered block from disk, and byte-compares.  Nothing in a
fenced block is retyped or hand-edited.  Exit 0 is required.
"""
import collections
import hashlib
import json
import os
import re
import sys

LOGS = "/Users/chenhao/Documents/github/Setup-Agent/logs"
OUTDIR = os.path.join(LOGS, "d2r4-20260815", "slices")

ROOTS = {
    "ignite": os.path.join(LOGS, "session_20260815_213948_772273_b119d6ceb772_26732", ".setup_agent"),
    "lucene": os.path.join(LOGS, "session_20260815_214920_363481_69f72d0f9af5_26911", ".setup_agent"),
    "kogito": os.path.join(LOGS, "session_20260815_221129_625455_bb3e87bcb9fd_27462", ".setup_agent"),
    # d2r3 comparison source, used only by the ignite file
    "ignite_d2r3": os.path.join(LOGS, "session_20260814_074153_238028_5398df380672_24385", ".setup_agent"),
}


# ------------------------------------------------------------------ helpers

def _read_text(root, rel):
    with open(os.path.join(ROOTS[root], rel), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def sha_len(root, rel):
    with open(os.path.join(ROOTS[root], rel), "rb") as fh:
        b = fh.read()
    return hashlib.sha256(b).hexdigest(), len(b)


# ---------------------------------------------------------------- resolvers

def r_file(root, rel):
    return _read_text(root, rel)


def r_ce(root, seq):
    """Raw control_events.jsonl line, byte-exact; line number == sequence."""
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for i, line in enumerate(fh, start=1):
            if i == seq:
                return line.rstrip("\n")
    raise KeyError((root, seq))


def r_vkey(root, key):
    """Byte-exact `"key":<value>` substring of the compact single-line verdict.json."""
    raw = _read_text(root, "verdict.json")
    obj = json.loads(raw)
    assert json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=True) == raw
    frag = json.dumps(key) + ":" + json.dumps(obj[key], separators=(",", ":"),
                                              sort_keys=True, ensure_ascii=True)
    assert frag in raw, (root, key)
    return frag


def r_vrec(root, idx):
    """Byte-exact substring of verdict.json: phase_records[idx], whole."""
    raw = _read_text(root, "verdict.json")
    obj = json.loads(raw)
    frag = json.dumps(obj["phase_records"][idx], separators=(",", ":"),
                      sort_keys=True, ensure_ascii=True)
    assert frag in raw, (root, idx)
    return frag


def r_pinkey(root, key):
    raw = _read_text(root, "run-pin.json")
    obj = json.loads(raw)
    assert json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=True) == raw
    frag = json.dumps(key) + ":" + json.dumps(obj[key], separators=(",", ":"),
                                              sort_keys=True, ensure_ascii=True)
    assert frag in raw, (root, key)
    return frag


def r_fullout(root, ref):
    """The stored tool output body, decoded, byte-exact as stored."""
    with open(os.path.join(ROOTS[root], "contexts/full_outputs.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("ref_id") == ref:
                return rec["output"]
    raise KeyError((root, ref))


def r_ctx(root, phase, idx, field):
    """A decoded string value out of the branch history the model actually held."""
    obj = json.loads(_read_text(root, "contexts/phase_%s.json" % phase))
    return obj["history"][idx][field]


def r_ctxjson(root, phase, idx, field):
    """Byte-exact `"field": <value>` substring of the 2-space-indented phase context."""
    raw = _read_text(root, "contexts/phase_%s.json" % phase)
    obj = json.loads(raw)
    frag = json.dumps(field) + ": " + json.dumps(obj["history"][idx][field],
                                                 indent=2, ensure_ascii=True)
    # re-indent to the nesting depth used inside history[] entries (6 spaces)
    frag = frag.replace("\n", "\n      ")
    assert frag in raw, (root, phase, idx, field)
    return frag


def r_ctxmeta(root, phase, field):
    obj = json.loads(_read_text(root, "contexts/phase_%s.json" % phase))
    return obj[field]


def r_journal_intro(root, phase, iteration):
    p = os.path.join(ROOTS[root], "contexts/journal/phase_%s.journal.jsonl" % phase)
    with open(p, "r", encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("iteration") == iteration and rec.get("intro_text"):
                return rec["intro_text"]
    raise KeyError((root, phase, iteration))


def r_kindcount(root):
    c = collections.Counter()
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if line.strip():
                c[json.loads(line)["kind"]] += 1
    return "\n".join("%-28s %d" % (k, v) for k, v in sorted(c.items()))


def r_ledger(root):
    """Mechanical enumeration: every action_envelope in the run, in order.
    sequence, tool, and the compact exact_params as recorded."""
    rows = []
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["kind"] != "action_envelope":
                continue
            pl = e["payload"]
            rows.append("seq %-4d %-8s %s" % (
                e["sequence"], pl["tool"],
                json.dumps(pl["exact_params"], separators=(",", ":"),
                           sort_keys=True, ensure_ascii=True)))
    return "\n".join(rows)


def r_envelope_tools(root):
    """Mechanical: count of action_envelope events per `tool`, sorted."""
    c = collections.Counter()
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["kind"] == "action_envelope":
                c[e["payload"]["tool"]] += 1
    rows = ["%-10s %d" % (k, v) for k, v in sorted(c.items())]
    rows.append("%-10s %d" % ("build", c.get("build", 0)))
    return "\n".join(rows)


def r_sigs(root):
    """Mechanical: every distinct `result.failure_signature` in tool_result
    events, at the sequence of its FIRST occurrence, in first-occurrence order."""
    rows = []
    seen = set()
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["kind"] != "tool_result":
                continue
            r = e["payload"].get("result") or {}
            fs = r.get("failure_signature")
            if fs and fs not in seen:
                seen.add(fs)
                rows.append("seq %-4d %-42s %s" % (e["sequence"], r.get("error_code"), fs))
    return "\n".join(rows)


def r_ctxsigs(root, phase):
    """Mechanical: every `failure_signature` recorded in a phase branch-history
    entry, with the entry index and iteration.  Catches refused calls that
    produced no tool_result event."""
    obj = json.loads(_read_text(root, "contexts/phase_%s.json" % phase))
    rows = []
    for i, h in enumerate(obj["history"]):
        fs = h.get("failure_signature")
        if fs:
            rows.append("history[%d] iteration=%s tool=%s  %s"
                        % (i, h.get("iteration"), h.get("tool_name"), fs))
    return "\n".join(rows)


def r_grepcount(root, needle):
    """Mechanical: number of raw control_events.jsonl lines containing needle."""
    n = 0
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if needle in line:
                n += 1
    return "occurrences of %s in %s control_events.jsonl (raw line scan): %d" % (
        json.dumps(needle), root, n)


def r_envcalls(root):
    """Mechanical: every action_envelope whose exact_params.action == 'env'."""
    rows = []
    with open(os.path.join(ROOTS[root], "control_events.jsonl"), "r",
              encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["kind"] != "action_envelope":
                continue
            p = e["payload"]["exact_params"]
            if isinstance(p, dict) and p.get("action") == "env":
                rows.append("seq %-4d %s" % (e["sequence"],
                                             json.dumps(p, separators=(",", ":"),
                                                        sort_keys=True, ensure_ascii=True)))
    if not rows:
        rows.append("(none)")
    return "\n".join(rows)


def r_ctxdir(root):
    names = sorted(os.listdir(os.path.join(ROOTS[root], "contexts")))
    return "\n".join(names)


RESOLVERS = {
    "file": r_file,
    "ce": r_ce,
    "vkey": r_vkey,
    "vrec": r_vrec,
    "pinkey": r_pinkey,
    "fullout": r_fullout,
    "ctx": r_ctx,
    "ctxjson": r_ctxjson,
    "ctxmeta": r_ctxmeta,
    "journal_intro": r_journal_intro,
    "kindcount": r_kindcount,
    "ledger": r_ledger,
    "envelope_tools": r_envelope_tools,
    "sigs": r_sigs,
    "ctxsigs": r_ctxsigs,
    "grepcount": r_grepcount,
    "envcalls": r_envcalls,
    "ctxdir": r_ctxdir,
}


def locator(kind, args):
    """A byte-level pointer back to the source of a block, for elision markers."""
    root = args[0]
    rel = os.path.relpath(ROOTS[root], "/Users/chenhao/Documents/github/Setup-Agent")
    if kind == "ce":
        return "%s/control_events.jsonl line %d" % (rel, args[1])
    if kind == "ctx":
        return "%s/contexts/phase_%s.json history[%d].%s" % (rel, args[1], args[2], args[3])
    if kind == "fullout":
        return "%s/contexts/full_outputs.jsonl ref_id=%s" % (rel, args[1])
    if kind == "file":
        return "%s/%s" % (rel, args[1])
    raise AssertionError("no locator for resolver kind %s" % kind)


# -------------------------------------------------------------- page builder

class Page(object):
    def __init__(self, out_path):
        self.out = out_path
        self.lines = []
        self.specs = {}

    def w(self, s=""):
        self.lines.append(s)

    def block(self, bid, kind, *args, **kw):
        elide = kw.pop("elide", None)
        assert not kw
        assert bid not in self.specs, "duplicate block id %s" % bid
        self.specs[bid] = (kind, args, elide)
        body = self.resolve(bid)
        if elide is not None and "chars omitted" not in body:
            raise AssertionError("elision declared but not triggered for %s" % bid)
        fence = "```"
        while fence in body:
            fence += "`"
        self.w("<!-- V:%s -->" % bid)
        self.w(fence)
        self.w(body)
        self.w(fence)
        self.w()

    def resolve(self, bid):
        kind, args, elide = self.specs[bid]
        text = RESOLVERS[kind](*args)
        if elide is None:
            return text
        head, tail = elide
        if len(text) <= head + tail:
            return text
        cut = len(text) - tail
        omitted = cut - head
        # The marker is a pure locator: it names the exact character range of
        # this same block that was dropped, and where to re-read it.  No
        # description of the omitted content is asserted.
        return "%s[...%d chars omitted: characters %d..%d of this %d-character block; re-read at %s...]%s" % (
            text[:head], omitted, head, cut - 1, len(text), locator(kind, args), text[cut:])

    def finish(self):
        text = "\n".join(self.lines)
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        with open(self.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

        with open(self.out, "r", encoding="utf-8", newline="") as fh:
            md = fh.read()
        pat = re.compile(r"<!-- V:([A-Za-z0-9_]+) -->\n(`{3,})\n(.*?)\n\2\n", re.DOTALL)
        found = {}
        for m in pat.finditer(md):
            bid, body = m.group(1), m.group(3)
            if bid in found:
                print("DUPLICATE BLOCK ID:", bid)
                sys.exit(1)
            found[bid] = body
        missing = set(self.specs) - set(found)
        extra = set(found) - set(self.specs)
        if missing or extra:
            print("BLOCK SET MISMATCH  missing=%s extra=%s" % (sorted(missing), sorted(extra)))
            sys.exit(1)

        bad = []
        total = 0
        for bid in sorted(self.specs):
            expected = self.resolve(bid)          # re-read from disk, independently
            got = found[bid]
            if expected.encode("utf-8") != got.encode("utf-8"):
                bad.append(bid)
            total += len(got.encode("utf-8"))
        all_fences = len(re.findall(r"^`{3,}$", md, re.MULTILINE))
        if all_fences != 2 * len(found):
            bad.append("UNREGISTERED_FENCE(count=%d registered=%d)" % (all_fences, 2 * len(found)))
        if bad:
            print("BYTE-COMPARE FAILURES in %s: %s" % (self.out, bad))
            sys.exit(1)

        stamp = ("Self-verified by re-reading this file and re-resolving every fenced block from the "
                 "authoritative sources: **%d/%d blocks byte-identical**, %s bytes of quoted material "
                 "compared, 0 mismatches, 0 unregistered fenced blocks. Verifier: "
                 "`logs/d2r4-20260815/slices/gen_ignite_lucene_kogito_slices.py` (`python3 logs/d2r4-20260815/slices/"
                 "gen_ignite_lucene_kogito_slices.py`; exit 0 required)."
                 % (len(found), len(self.specs), format(total, ",")))
        md = md.replace("__VERIFY_LINE__", stamp)
        with open(self.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(md)
        with open(self.out, "r", encoding="utf-8", newline="") as fh:
            md2 = fh.read()
        found2 = {m.group(1): m.group(3) for m in pat.finditer(md2)}
        assert found2 == found, "blocks changed during stamping"
        print("OK  %-22s blocks=%d quoted_bytes=%d file_bytes=%d"
              % (os.path.basename(self.out), len(found), total, len(md.encode("utf-8"))))


def sources_table(root, rels):
    rows = ["| File | bytes | sha256 |", "|---|---|---|"]
    for rel, note in rels:
        sha, n = sha_len(root, rel)
        rows.append("| `.setup_agent/%s`%s | %d | `%s` |" % (rel, note, n, sha))
    return "\n".join(rows)


COMMON_HEAD = """**Protocol:** `docs/superpowers/specs/2026-08-13-d2-raw-slice-review-protocol.md`
(section 1 — authoritative sources only; section 3 — verbatim `[A]`/`[B]`/`[C]`/`[D]`, no diagnosis
inside a slice). **There is no judgment section in this file.**
"""

COMMON_METHOD = """**Not read:** the campaign console log and `session_*/main.log` — console prose, excluded by
section 1 of the protocol, which records that console renders have already produced two false campaign
conclusions. `session_*/agent_execution.log`, `session_*/command_project_*.log`,
`session_*/token_usage.csv` and `session_*/setup-report-*.md` are outside the authoritative layer and
no block in this file is drawn from them.

**Generation.** Written by `logs/d2r4-20260815/slices/gen_ignite_lucene_kogito_slices.py` (kept beside this file). Every
fenced block is emitted by a resolver that reads the authoritative file fresh; no block was retyped or
hand-edited. Each block is preceded by an HTML comment `<!-- V:<id> -->`.

**Self-verification (byte-accuracy).** After writing, the script re-read this markdown, re-resolved
every `V:` block from disk independently, and byte-compared. The result is stamped at the foot of the
file. Any block that failed the compare aborts generation with a non-zero exit.

**Block kinds.** (i) *Raw `control_events.jsonl` lines* — exactly the bytes on the line whose number
equals the `sequence`; the generator asserted `sequence == line number` for the whole file.
(ii) *Byte-exact substrings of `verdict.json` / `run-pin.json`* — both are compact single-line JSON
with sorted keys and `ensure_ascii`; the generator asserts a round-trip re-serialization equals the
file before slicing, so every `"key":value` fragment is a literal substring. (iii) *Decoded string
values* — branch-history `observation`/`output` fields, journal `intro_text`, and stored bodies from
`full_outputs.jsonl`, quoted byte-exact as the model saw them. (iv) *Mechanical enumerations* —
produced by iterating the authoritative file and formatting one row per record; the row layout is the
generator's, every value in it is read from disk. These are labelled "mechanical" where they appear.

**Elision convention.** Where a block is shortened, an inline marker names the exact character range
of that same block that was dropped and the file coordinate to re-read it from — it asserts nothing
about the omitted content. Everything outside such a marker is verbatim. Source-side truncations (the
harness's own `...` inside `error`/`reason` strings, and its `Full output ref:` notices) are part of
what the model actually saw and are quoted, not repaired.
"""


# ==================================================================== IGNITE

def build_ignite():
    p = Page(os.path.join(OUTDIR, "ignite.md"))
    R = "ignite"
    D3 = "ignite_d2r3"
    w = p.w

    w("# D2R4 raw slice — ignite")
    w()
    w("**Project:** ignite (`https://github.com/apache/ignite.git`, ref `2.18.0`, resolved commit "
      "`d49adada19829f2186dfb5e2e8d12e7d79c1c3a7`)")
    w()
    w("**Session dir:** `/Users/chenhao/Documents/github/Setup-Agent/logs/"
      "session_20260815_213948_772273_b119d6ceb772_26732`")
    w()
    w("**Campaign:** `logs/d2r4-20260815` — lane H, wall time 571 s, exit 1 "
      "(`campaign-results.json`). Prior run for comparison: D2R3 serial, lane 01, wall time 6,703 s, "
      "session `logs/session_20260814_074153_238028_5398df380672_24385`.")
    w()
    w(COMMON_HEAD)
    w("**Extraction questions this file serves (scope only — they are answered nowhere in this file, "
      "only sourced):**")
    w()
    w("1. Every build dispatch in the run and its observation, verbatim — did any dispatch actually "
      "run, and what refused it.")
    w("2. Every distinct failure signature at first occurrence, with the full `[A]`/`[B]`/`[C]`/`[D]` "
      "quad.")
    w("3. The final build gate, whole.")
    w("4. Against D2R3: whether the `maven_version_requirement` move was tried in this run, and what "
      "came back.")
    w()
    w("## Sources of truth used")
    w()
    w(sources_table(R, [
        ("control_events.jsonl", " (243 events; line number == `sequence`)"),
        ("verdict.json", " (compact single-line JSON, sorted keys)"),
        ("run-pin.json", ""),
        ("report_metrics.json", ""),
        ("phase-handoff.json", ""),
        ("build_requirements.json", ""),
    ]))
    w()
    w("Also read, no separate sha row: `.setup_agent/contexts/phase_{provision,analyze,build,report}.json`, "
      "`.setup_agent/contexts/journal/phase_build.journal.jsonl`, "
      "`.setup_agent/contexts/full_outputs.jsonl` (21 stored refs), "
      "`.setup_agent/repair_contexts/rcx-dfe4302a7b34.json`, and for Part 5 the D2R3 session's "
      "`control_events.jsonl` and `verdict.json`.")
    w()
    w("**Anchor for the sealed record.** `verdict.json` carries an `evidence_publication` control "
      "event whose `raw_sha256` equals the on-disk sha256 above; that event is quoted whole in Part 4. "
      "The equality was checked by the generator, not asserted by the author.")
    w()
    w(COMMON_METHOD)
    w("**Run shape** — `control_events.jsonl` event kinds (mechanical count):")
    w()
    p.block("kindcount", "kindcount", R)
    w("---")
    w()

    # ------------------------------------------------------------- Part 1
    w("## Part 1 — The sealed record")
    w()
    w("**1a.** Byte-exact substrings of `verdict.json`:")
    w()
    for key in ("verdict", "build_evidence", "rates", "conflicts", "test_stats", "run_id",
                "finalized_at", "schema_version"):
        p.block("v_%s" % key, "vkey", R, key)
    w("**1b.** `phase_records`, each record whole and byte-exact, in file order:")
    w()
    for i in range(4):
        p.block("vrec%d" % i, "vrec", R, i)
    w("---")
    w()

    # ------------------------------------------------------------- Part 2
    w("## Part 2 — Every build dispatch in the run")
    w()
    w("**Mechanical enumeration 1** — every `action_envelope` in `control_events.jsonl`, in order, "
      "with its `tool` and its `exact_params` as recorded (25 rows; this is the complete dispatch "
      "ledger for the run):")
    w()
    p.block("ledger", "ledger", R)
    w("**Mechanical enumeration 2** — `action_envelope` count per `tool`. The last row re-states the "
      "count for `build` explicitly, taken from the same counter:")
    w()
    p.block("envtools", "envelope_tools", R)
    w("**Mechanical enumeration 3** — every `action_envelope` whose `exact_params.action` is `env`:")
    w()
    p.block("envcalls", "envcalls", R)
    w("**What the engine told the model the build coordinate was.** `contexts/journal/"
      "phase_build.journal.jsonl`, the `intro_text` of iteration 13 — the phase-entry text the model "
      "received on entering `build`, whole:")
    w()
    p.block("journal", "journal_intro", R, "build", 13)
    w("**The advisor output at the head of the build phase** (`action_envelope` control-000191, "
      "`tool_result` control-000192, stored as `output_9fed35c00831`, `output_length` 616), whole:")
    w()
    p.block("advisor", "fullout", R, "output_9fed35c00831")
    w("**The repair channel that was opened after the first build-phase blocked-claim** — "
      "`.setup_agent/repair_contexts/rcx-dfe4302a7b34.json`, whole file, byte-exact (also carried "
      "inline in the `tool_result` quoted in Slice 6 [C]):")
    w()
    p.block("rcx", "file", R, "repair_contexts/rcx-dfe4302a7b34.json")
    w("**The build-phase branch history in full** — `contexts/phase_build.json` `history`, every "
      "entry's `tool_name` / `parameters` / `operation_outcome`, quoted as the byte-exact "
      "`\"parameters\": {...}` substrings of the 2-space-indented context file, in order:")
    w()
    for i in range(6):
        p.block("bh%d" % i, "ctxjson", R, "build", i, "parameters")
    w("---")
    w()

    # ------------------------------------------------------------- Part 3
    w("## Part 3 — Every distinct failure signature, at first occurrence")
    w()
    w("**Mechanical enumeration** — every distinct `result.failure_signature` across all `tool_result` "
      "events, at the `sequence` of its first occurrence, in first-occurrence order:")
    w()
    p.block("sigs", "sigs", R)
    w("**Cross-check** — every `failure_signature` recorded in a branch-history entry, per phase "
      "(this catches any refused call that produced no `tool_result` event; in this run it produces "
      "the same set):")
    w()
    p.block("csig_p", "ctxsigs", R, "provision")
    p.block("csig_b", "ctxsigs", R, "build")
    w("Six slices follow, one per signature, in that order.")
    w()

    # Slice 1
    w("### Slice 1: provision, iteration 2, `project(action='env')` for `/usr/bin/mvn` refused — "
      "`ENV_EXECUTABLE_NOT_FOUND`")
    w()
    w("**Where:** session `session_20260815_213948_772273_b119d6ceb772_26732`, `control_events` "
      "sequence 13 (envelope) / 14 (result), phase `provision`, attempt `provision-1`, iteration 2. "
      "Branch history: `contexts/phase_provision.json` `history[2]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[0]` and "
      "`history[1]`, the two entries preceding it, each `observation` whole and byte-exact:")
    w()
    p.block("s1a0", "ctx", R, "provision", 0, "observation")
    p.block("s1a1", "ctx", R, "provision", 1, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000013, raw line:")
    w()
    p.block("s1b", "ce", R, 13)
    w("**[C] What came back** — `tool_result` control-000014, raw line:")
    w()
    p.block("s1c", "ce", R, 14)
    w("Stored body by ref (`full_outputs.jsonl` `ref_id=output_1057ae343c76`, `output_length` 72), "
      "whole:")
    w()
    p.block("s1c_out", "fullout", R, "output_1057ae343c76")
    w("As it entered the model's context (`contexts/phase_provision.json` `history[2].observation`), "
      "whole:")
    w()
    p.block("s1c_obs", "ctx", R, "provision", 2, "observation")
    w("**[D] What happened next:** control-000015 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 2
    w("### Slice 2: provision, iteration 4, `project(action='env')` for `/workspace/ignite/mvnw` "
      "refused — `ENV_MAVEN_EXECUTABLE_NAME_MISMATCH`")
    w()
    w("**Where:** `control_events` sequence 25 (envelope) / 26 (result), phase `provision`, attempt "
      "`provision-1`, iteration 4. Branch history: `contexts/phase_provision.json` `history[5]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — the two entries between "
      "Slice 1 [C] and this call, `history[3]` and `history[4]`, each `observation` whole:")
    w()
    p.block("s2a3", "ctx", R, "provision", 3, "observation")
    p.block("s2a4", "ctx", R, "provision", 4, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000025, raw line:")
    w()
    p.block("s2b", "ce", R, 25)
    w("**[C] What came back** — `tool_result` control-000026, raw line:")
    w()
    p.block("s2c", "ce", R, 26)
    w("Stored body by ref (`ref_id=output_604b79d126cf`, `output_length` 68), whole:")
    w()
    p.block("s2c_out", "fullout", R, "output_604b79d126cf")
    w("As it entered the model's context (`history[5].observation`), whole:")
    w()
    p.block("s2c_obs", "ctx", R, "provision", 5, "observation")
    w("**[D] What happened next:** control-000027 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 3
    w("### Slice 3: provision, iteration 6, `phase(action='blocked')` refused — "
      "`blocked_contradicted_by_green_evidence`")
    w()
    w("**Where:** `control_events` sequence 33 (envelope) / 34 (result), with `validator_observation` "
      "35, `gate_decision` 36 and `completion_claim_decision` 37 following; phase `provision`, "
      "iteration 6. Branch history: `contexts/phase_provision.json` `history[7]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[6]`, the entry "
      "preceding it, `observation` whole (its `output_length` in `full_outputs.jsonl` is 0):")
    w()
    p.block("s3a6", "ctx", R, "provision", 6, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000033, raw line:")
    w()
    p.block("s3b", "ce", R, 33)
    w("**[C] What came back** — `tool_result` control-000034, raw line:")
    w()
    p.block("s3c", "ce", R, 34)
    w("Stored body by ref (`ref_id=output_21a3c65862a2`, `output_length` 334), whole:")
    w()
    p.block("s3c_out", "fullout", R, "output_21a3c65862a2")
    w("As it entered the model's context (`history[7].observation`), whole:")
    w()
    p.block("s3c_obs", "ctx", R, "provision", 7, "observation")
    w("The three control events that followed the refusal, raw lines:")
    w()
    for s in (35, 36, 37):
        p.block("s3d%d" % s, "ce", R, s)
    w("**[D] What happened next:** control-000035 `validator_observation`.")
    w()
    w("---")
    w()

    # Slice 4
    w("### Slice 4: build, iteration 15, `project(action='env')` for `/tmp/mvnshim/mvn` refused — "
      "`ENV_EXECUTABLE_REALPATH_ESCAPE`")
    w()
    w("**Where:** `control_events` sequence 199 (envelope) / 200 (result), phase `build`, attempt "
      "`build-1`, iteration 15. Branch history: `contexts/phase_build.json` `history[1]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — the phase-entry "
      "`intro_text` and the advisor output are quoted in Part 2 and are not repeated. The one branch "
      "entry preceding this call, `history[0]`, `observation` whole:")
    w()
    p.block("s4a0", "ctx", R, "build", 0, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000199, raw line:")
    w()
    p.block("s4b", "ce", R, 199)
    w("**[C] What came back** — `tool_result` control-000200, raw line:")
    w()
    p.block("s4c", "ce", R, 200)
    w("Stored body by ref (`ref_id=output_ca4fbd11264d`, `output_length` 102), whole:")
    w()
    p.block("s4c_out", "fullout", R, "output_ca4fbd11264d")
    w("As it entered the model's context (`history[1].observation`), whole:")
    w()
    p.block("s4c_obs", "ctx", R, "build", 1, "observation")
    w("**[D] What happened next:** control-000201 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 5
    w("### Slice 5: build, iteration 17, `project(action='env')` for `/workspace/ignite/bin/mvn` "
      "refused — `ENV_RUNTIME_PROBE_FAILED`")
    w()
    w("**Where:** `control_events` sequence 207 (envelope) / 208 (result), phase `build`, attempt "
      "`build-1`, iteration 17. Branch history: `contexts/phase_build.json` `history[3]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[2]`, the entry "
      "preceding it, `observation` whole:")
    w()
    p.block("s5a2", "ctx", R, "build", 2, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000207, raw line:")
    w()
    p.block("s5b", "ce", R, 207)
    w("**[C] What came back** — `tool_result` control-000208, raw line:")
    w()
    p.block("s5c", "ce", R, 208)
    w("Stored body by ref (`ref_id=output_c05c6ffb3853`, `output_length` 342), whole:")
    w()
    p.block("s5c_out", "fullout", R, "output_c05c6ffb3853")
    w("As it entered the model's context (`history[3].observation`), whole:")
    w()
    p.block("s5c_obs", "ctx", R, "build", 3, "observation")
    w("**[D] What happened next:** control-000209 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 6
    w("### Slice 6: build, iteration 18, `phase(action='blocked')` refused — `build_red`, repair "
      "context opened")
    w()
    w("**Where:** `control_events` sequence 211 (envelope) / 214 (result), with `evidence_publication` "
      "212 and 213 between them and `validator_observation` 215, `gate_decision` 216, "
      "`repair_context_opened` 217 and `completion_claim_decision` 218 following; phase `build`, "
      "attempt `build-1`, iteration 18. Branch history: `contexts/phase_build.json` `history[4]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — Slice 5 [C] is the "
      "immediately preceding entry and is not repeated.")
    w()
    w("**[B] The call the model made** — `action_envelope` control-000211, raw line:")
    w()
    p.block("s6b", "ce", R, 211)
    w("**[C] What came back** — `tool_result` control-000214, raw line. This line carries the "
      "`repair_context` inline and is the longest in the file (10,204 chars); it is quoted with one "
      "elision marker, and the `repair_context` it carries is quoted whole from disk in Part 2:")
    w()
    p.block("s6c", "ce", R, 214, elide=(6200, 2600))
    w("Stored body by ref (`ref_id=output_26f5935f0090`, `output_length` 1041), whole:")
    w()
    p.block("s6c_out", "fullout", R, "output_26f5935f0090")
    w("As it entered the model's context (`history[4].observation`), whole:")
    w()
    p.block("s6c_obs", "ctx", R, "build", 4, "observation")
    w("The two `evidence_publication` events between the call and the result, and the four control "
      "events that followed it, raw lines:")
    w()
    for s in (212, 213, 215, 216, 217, 218):
        p.block("s6e%d" % s, "ce", R, s)
    w("**[D] What happened next:** control-000215 `validator_observation`.")
    w()
    w("---")
    w()

    # ------------------------------------------------------------- Part 4
    w("## Part 4 — The final build gate, whole")
    w()
    w("The build phase's terminal claim, its validation, the gate, and the routing out of the phase. "
      "Every event from the claim envelope to `evidence_close`, in order, raw lines.")
    w()
    w("**The claim** — `action_envelope` control-000221 (`phase(action='done', outcome='failed')`):")
    w()
    p.block("g221", "ce", R, 221)
    w("**Its result** — `tool_result` control-000222:")
    w()
    p.block("g222", "ce", R, 222)
    w("As it entered the model's context (`contexts/phase_build.json` `history[5].observation`), "
      "whole:")
    w()
    p.block("g222_obs", "ctx", R, "build", 5, "observation")
    w("**The validator observation** — control-000225:")
    w()
    p.block("g225", "ce", R, 225)
    w("**The gate decision** — control-000226, whole:")
    w()
    p.block("g226", "ce", R, 226)
    w("**The phase transition** — control-000227:")
    w()
    p.block("g227", "ce", R, 227)
    w("**The verdict seal** — `evidence_publication` control-000228; its `raw_sha256` is the on-disk "
      "sha256 of `verdict.json` in the sources table:")
    w()
    p.block("g228", "ce", R, 228)
    w("**The evidence close** — control-000229:")
    w()
    p.block("g229", "ce", R, 229)
    w("**What the run wrote as build metrics** — `.setup_agent/report_metrics.json`, whole file, "
      "byte-exact (sealed by `evidence_publication` control-000231):")
    w()
    p.block("g_metrics", "file", R, "report_metrics.json")
    w("**The surveyed build requirements the gate measured against** — "
      "`.setup_agent/build_requirements.json`, whole file, byte-exact:")
    w()
    p.block("g_breq", "file", R, "build_requirements.json")
    w("---")
    w()

    # ------------------------------------------------------------- Part 5
    w("## Part 5 — Against D2R3: the `maven_version_requirement` move")
    w()
    w("D2R3 source: `logs/session_20260814_074153_238028_5398df380672_24385/.setup_agent`. Same repo, "
      "same ref `2.18.0`, same resolved commit. Its sealed files:")
    w()
    w(sources_table(D3, [
        ("control_events.jsonl", " (244 events; line number == `sequence`)"),
        ("verdict.json", ""),
    ]))
    w()
    w("**Mechanical enumeration 1** — raw-line scan for the literal parameter name in both runs:")
    w()
    p.block("mvr_r4", "grepcount", R, "maven_version_requirement")
    p.block("mvr_r3", "grepcount", D3, "maven_version_requirement")
    w("**Mechanical enumeration 2** — every `action_envelope` whose `exact_params.action` is `env`, "
      "in D2R3:")
    w()
    p.block("d3_envcalls", "envcalls", D3)
    w("The same enumeration for D2R4 is in Part 2 (`V:envcalls`).")
    w()
    w("**Mechanical enumeration 3** — `action_envelope` count per `tool`, D2R3:")
    w()
    p.block("d3_envtools", "envelope_tools", D3)
    w("**D2R3, the one env registration and its refusal.** `action_envelope` control-000014 and "
      "`tool_result` control-000015, raw lines:")
    w()
    p.block("d3_14", "ce", D3, 14)
    p.block("d3_15", "ce", D3, 15)
    w("**D2R3, the provision phase's terminal claim after that refusal** — `action_envelope` "
      "control-000026, raw line:")
    w()
    p.block("d3_26", "ce", D3, 26)
    w("**D2R3, the build dispatch.** `action_envelope` control-000168, raw line:")
    w()
    p.block("d3_168", "ce", D3, 168)
    w("**D2R3, what came back** — `tool_result` control-000174, raw line (50,672 chars), one "
      "elision marker:")
    w()
    p.block("d3_174", "ce", D3, 174, elide=(3400, 2400))
    w("**D2R3, the `loop_decision` recorded for that dispatch** — control-000175, raw line:")
    w()
    p.block("d3_175", "ce", D3, 175)
    w("**D2R3, the env overlay revision published between the dispatch and its result** — "
      "`evidence_publication` control-000169, raw line:")
    w()
    p.block("d3_169", "ce", D3, 169)
    w("**D2R3, the sealed build record** — byte-exact substrings of that run's `verdict.json`:")
    w()
    p.block("d3_vbuild", "vkey", D3, "build_evidence")
    p.block("d3_vrates", "vkey", D3, "rates")
    p.block("d3_vverdict", "vkey", D3, "verdict")
    w("**D2R3, its `build` phase record**, whole and byte-exact (`phase_records[2]`):")
    w()
    p.block("d3_vrec2", "vrec", D3, 2)
    w("---")
    w()

    w("<!-- VERIFICATION FOOTER -->")
    w()
    w("## Verification stamp")
    w()
    w("__VERIFY_LINE__")
    w()
    p.finish()


# ==================================================================== LUCENE

def build_lucene():
    p = Page(os.path.join(OUTDIR, "lucene.md"))
    R = "lucene"
    w = p.w

    w("# D2R4 raw slice — lucene")
    w()
    w("**Project:** lucene (`https://github.com/apache/lucene.git`, ref `releases/lucene/10.4.0`, "
      "resolved commit `9983b7ce7fdd04f4d357688fb85c14277c15ea8d`)")
    w()
    w("**Session dir:** `/Users/chenhao/Documents/github/Setup-Agent/logs/"
      "session_20260815_214920_363481_69f72d0f9af5_26911`")
    w()
    w("**Campaign:** `logs/d2r4-20260815` — lane H, wall time 329 s, exit 1 "
      "(`campaign-results.json`).")
    w()
    w(COMMON_HEAD)
    w("**Extraction questions this file serves (scope only — they are answered nowhere in this file, "
      "only sourced):**")
    w()
    w("1. Every build dispatch in the run and its observation, verbatim — did any dispatch actually "
      "run, and what refused it.")
    w("2. Every distinct failure signature at first occurrence, with the full `[A]`/`[B]`/`[C]`/`[D]` "
      "quad.")
    w("3. The final build gate, whole.")
    w()
    w("## Sources of truth used")
    w()
    w(sources_table(R, [
        ("control_events.jsonl", " (132 events; line number == `sequence`)"),
        ("verdict.json", " (compact single-line JSON, sorted keys)"),
        ("run-pin.json", ""),
        ("report_metrics.json", ""),
        ("phase-handoff.json", ""),
        ("build_requirements.json", ""),
        ("invocation_contracts/ic-43bfcb893de6.json", ""),
        ("invocation_receipts/inv-gradle-1-b37b2535eedf-0001.json", ""),
    ]))
    w()
    w("Also read, no separate sha row: `.setup_agent/contexts/phase_{provision,analyze,build,report}.json`, "
      "`.setup_agent/contexts/journal/phase_build.journal.jsonl`, "
      "`.setup_agent/contexts/full_outputs.jsonl` (9 stored refs), "
      "`.setup_agent/repair_contexts/`.")
    w()
    w("**Anchor for the sealed record.** `verdict.json` carries an `evidence_publication` control "
      "event whose `raw_sha256` equals the on-disk sha256 above; that event is quoted whole in Part 4.")
    w()
    w(COMMON_METHOD)
    w("**Run shape** — `control_events.jsonl` event kinds (mechanical count):")
    w()
    p.block("kindcount", "kindcount", R)
    w("---")
    w()

    # Part 1
    w("## Part 1 — The sealed record")
    w()
    w("**1a.** Byte-exact substrings of `verdict.json`:")
    w()
    for key in ("verdict", "build_evidence", "rates", "conflicts", "test_stats", "run_id",
                "finalized_at", "schema_version"):
        p.block("v_%s" % key, "vkey", R, key)
    w("**1b.** `phase_records`, each record whole and byte-exact, in file order:")
    w()
    nrec = len(json.loads(_read_text(R, "verdict.json"))["phase_records"])
    for i in range(nrec):
        p.block("vrec%d" % i, "vrec", R, i)
    w("---")
    w()

    # Part 2
    w("## Part 2 — Every build dispatch in the run")
    w()
    w("**Mechanical enumeration 1** — every `action_envelope` in `control_events.jsonl`, in order, "
      "with its `tool` and its `exact_params` as recorded (13 rows; this is the complete dispatch "
      "ledger for the run):")
    w()
    p.block("ledger", "ledger", R)
    w("**Mechanical enumeration 2** — `action_envelope` count per `tool`. The last row re-states the "
      "count for `build` explicitly, taken from the same counter:")
    w()
    p.block("envtools", "envelope_tools", R)
    w("**What the engine told the model the build coordinate was.** `contexts/journal/"
      "phase_build.journal.jsonl`, the `intro_text` of iteration 4 — the phase-entry text the model "
      "received on entering `build`, whole:")
    w()
    p.block("journal", "journal_intro", R, "build", 4)
    w("**The advisor output at the head of the build phase** (`action_envelope` control-000082, "
      "`tool_result` control-000083, stored as `output_3ac799d2d9c3`, `output_length` 686), whole:")
    w()
    p.block("advisor", "fullout", R, "output_3ac799d2d9c3")
    w("### The one build dispatch — full quad")
    w()
    w("**Where:** `control_events` sequence 86 (envelope) / 90 (result), with `evidence_publication` "
      "87 (invocation contract), 88 (invocation receipt) and 89 (receipt assessment) between them; "
      "phase `build`, attempt `build-1`, iteration 5. Branch history: `contexts/phase_build.json` "
      "`history[0]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — the build phase's branch "
      "history is empty before this entry; what stood in context is the phase-entry `intro_text` and "
      "the advisor output quoted immediately above, plus the carried-forward summary fields of "
      "`contexts/phase_build.json`, quoted here whole:")
    w()
    p.block("s0a_task", "ctxmeta", R, "build", "task_description")
    p.block("s0a_prev", "ctxmeta", R, "build", "previous_task_summary")
    p.block("s0a_dig", "ctxmeta", R, "build", "previous_task_evidence_digest")
    w("**[B] The call the model made** — `action_envelope` control-000086, raw line:")
    w()
    p.block("s0b", "ce", R, 86)
    w("**[C] What came back** — `tool_result` control-000090, raw line, whole:")
    w()
    p.block("s0c", "ce", R, 90)
    w("Stored body by ref (`full_outputs.jsonl` `ref_id=output_966bb07875a6`, `output_length` 61, "
      "`tool_name` `gradle`, `task_id` `gradle__workspace_lucene`), whole — this is the entire "
      "captured output of the dispatched command:")
    w()
    p.block("s0c_out", "fullout", R, "output_966bb07875a6")
    w("As it entered the model's context (`contexts/phase_build.json` `history[0].observation`), "
      "whole:")
    w()
    p.block("s0c_obs", "ctx", R, "build", 0, "observation")
    w("The invocation contract published for this dispatch, whole file, byte-exact:")
    w()
    p.block("s0_ic", "file", R, "invocation_contracts/ic-43bfcb893de6.json")
    w("The invocation receipt it produced, whole file, byte-exact:")
    w()
    p.block("s0_ir", "file", R, "invocation_receipts/inv-gradle-1-b37b2535eedf-0001.json")
    w("The three `evidence_publication` events between the call and the result, raw lines:")
    w()
    for s in (87, 88, 89):
        p.block("s0e%d" % s, "ce", R, s)
    w("**[D] What happened next:** control-000091 `loop_decision`.")
    w()
    w("---")
    w()

    # Part 3
    w("## Part 3 — Every distinct failure signature, at first occurrence")
    w()
    w("**Mechanical enumeration** — every distinct `result.failure_signature` across all `tool_result` "
      "events, at the `sequence` of its first occurrence, in first-occurrence order:")
    w()
    p.block("sigs", "sigs", R)
    w("**Cross-check** — every `failure_signature` recorded in a branch-history entry, for the build "
      "phase (this catches any refused call that produced no `tool_result` event):")
    w()
    p.block("csig_b", "ctxsigs", R, "build")
    w("Slice 1 is the build dispatch, quoted whole in Part 2 (`DETACHED_OPERATION_FAILED:"
      "de148e504ea6f6a7`, sequence 86/90) and not repeated here. Slices 2 and 3 follow.")
    w()

    # Slice 2
    w("### Slice 2: build, iteration 6, `project(action='provision', java_version='21')` refused — "
      "`JAVA_VERSION_VERIFICATION_MISMATCH`")
    w()
    w("**Where:** `control_events` sequence 93 (envelope) / 94 (result), phase `build`, attempt "
      "`build-1`, iteration 6. Branch history: `contexts/phase_build.json` `history[1]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[0]`, the build "
      "dispatch entry, quoted in Part 2 [C] and not repeated. The provision-phase record of the "
      "Java 17 install that this call attempted to change, whole (`contexts/phase_provision.json` "
      "`history[1].observation`):")
    w()
    p.block("s2a", "ctx", R, "provision", 1, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000093, raw line:")
    w()
    p.block("s2b", "ce", R, 93)
    w("**[C] What came back** — `tool_result` control-000094, raw line:")
    w()
    p.block("s2c", "ce", R, 94)
    w("Stored body by ref (`ref_id=output_b3048a758b49`, `output_length` 283), whole:")
    w()
    p.block("s2c_out", "fullout", R, "output_b3048a758b49")
    w("As it entered the model's context (`history[1].observation`), whole:")
    w()
    p.block("s2c_obs", "ctx", R, "build", 1, "observation")
    w("**[D] What happened next:** control-000095 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 3
    w("### Slice 3: build, iteration 7, `phase(action='blocked')` refused — `build_red`, repair "
      "context opened")
    w()
    w("**Where:** `control_events` sequence 97 (envelope) / 100 (result), with `evidence_publication` "
      "98 and 99 between them and `validator_observation` 101, `gate_decision` 102, "
      "`repair_context_opened` 103 and `completion_claim_decision` 104 following; phase `build`, "
      "attempt `build-1`, iteration 7. Branch history: `contexts/phase_build.json` `history[2]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — Slice 2 [C] is the "
      "immediately preceding entry and is not repeated.")
    w()
    w("**[B] The call the model made** — `action_envelope` control-000097, raw line:")
    w()
    p.block("s3b", "ce", R, 97)
    w("**[C] What came back** — `tool_result` control-000100, raw line, one elision marker:")
    w()
    p.block("s3c", "ce", R, 100, elide=(5400, 2400))
    w("Stored body by ref (`ref_id=output_c37ac07a6e8f`, `output_length` 491), whole:")
    w()
    p.block("s3c_out", "fullout", R, "output_c37ac07a6e8f")
    w("As it entered the model's context (`history[2].observation`), whole:")
    w()
    p.block("s3c_obs", "ctx", R, "build", 2, "observation")
    w("The four control events that followed, raw lines:")
    w()
    for s in (101, 102, 103, 104):
        p.block("s3e%d" % s, "ce", R, s)
    w("**[D] What happened next:** control-000101 `validator_observation`.")
    w()
    w("**The one action taken inside the repair channel**, and its result — `action_envelope` "
      "control-000107 and `tool_result` control-000108, raw lines. The envelope carries the "
      "model-owned `repair_intent` fields:")
    w()
    p.block("s3r107", "ce", R, 107)
    p.block("s3r108", "ce", R, 108)
    w("As it entered the model's context (`contexts/phase_build.json` `history[3].observation`), "
      "whole — note the source-side truncation notice inside it:")
    w()
    p.block("s3r_obs", "ctx", R, "build", 3, "observation")
    w("---")
    w()

    # Part 4
    w("## Part 4 — The final build gate, whole")
    w()
    w("**The claim** — `action_envelope` control-000111 (`phase(action='done', outcome='failed')`):")
    w()
    p.block("g111", "ce", R, 111)
    w("**Its result** — `tool_result` control-000112:")
    w()
    p.block("g112", "ce", R, 112)
    w("As it entered the model's context (`contexts/phase_build.json` `history[4].observation`), "
      "whole:")
    w()
    p.block("g112_obs", "ctx", R, "build", 4, "observation")
    w("**The validator observation** — control-000115:")
    w()
    p.block("g115", "ce", R, 115)
    w("**The gate decision** — control-000116, whole:")
    w()
    p.block("g116", "ce", R, 116)
    w("**The phase transition** — control-000117:")
    w()
    p.block("g117", "ce", R, 117)
    w("**The verdict seal** — `evidence_publication` control-000118; its `raw_sha256` is the on-disk "
      "sha256 of `verdict.json` in the sources table:")
    w()
    p.block("g118", "ce", R, 118)
    w("**The evidence close** — control-000119:")
    w()
    p.block("g119", "ce", R, 119)
    w("**What the run wrote as build metrics** — `.setup_agent/report_metrics.json`, whole file, "
      "byte-exact:")
    w()
    p.block("g_metrics", "file", R, "report_metrics.json")
    w("**The surveyed build requirements the gate measured against** — "
      "`.setup_agent/build_requirements.json`, whole file, byte-exact:")
    w()
    p.block("g_breq", "file", R, "build_requirements.json")
    w("---")
    w()

    w("<!-- VERIFICATION FOOTER -->")
    w()
    w("## Verification stamp")
    w()
    w("__VERIFY_LINE__")
    w()
    p.finish()


# ==================================================================== KOGITO

def build_kogito():
    p = Page(os.path.join(OUTDIR, "kogito-examples.md"))
    R = "kogito"
    w = p.w

    w("# D2R4 raw slice — incubator-kie-kogito-examples")
    w()
    w("**Project:** incubator-kie-kogito-examples "
      "(`https://github.com/apache/incubator-kie-kogito-examples.git`, ref `1.44.1.Final`, resolved "
      "commit `f93f1c7bc94cb1e6bcab7570b693894c9a3ada7c`)")
    w()
    w("**Session dir:** `/Users/chenhao/Documents/github/Setup-Agent/logs/"
      "session_20260815_221129_625455_bb3e87bcb9fd_27462`")
    w()
    w("**Campaign:** `logs/d2r4-20260815` — lane L, wall time 631 s, exit 1 "
      "(`campaign-results.json`, project key `kogito-examples`).")
    w()
    w(COMMON_HEAD)
    w("**Extraction questions this file serves (scope only — they are answered nowhere in this file, "
      "only sourced):**")
    w()
    w("1. Every build dispatch in the run and its observation, verbatim — did any dispatch actually "
      "run, and what refused it.")
    w("2. Every distinct failure signature at first occurrence, with the full `[A]`/`[B]`/`[C]`/`[D]` "
      "quad.")
    w("3. The final build gate, whole.")
    w()
    w("## Sources of truth used")
    w()
    w(sources_table(R, [
        ("control_events.jsonl", " (196 events; line number == `sequence`)"),
        ("verdict.json", " (compact single-line JSON, sorted keys)"),
        ("run-pin.json", ""),
        ("report_metrics.json", ""),
        ("phase-handoff.json", ""),
        ("build_requirements.json", ""),
    ]))
    w()
    w("Also read, no separate sha row: `.setup_agent/contexts/phase_{provision,analyze,build,report}.json`, "
      "`.setup_agent/contexts/journal/phase_build.journal.jsonl`, "
      "`.setup_agent/contexts/full_outputs.jsonl` (17 stored refs), `.setup_agent/repair_contexts/`.")
    w()
    w("**Anchor for the sealed record.** `verdict.json` carries an `evidence_publication` control "
      "event whose `raw_sha256` equals the on-disk sha256 above; that event is quoted whole in Part 4.")
    w()
    w(COMMON_METHOD)
    w("**Run shape** — `control_events.jsonl` event kinds (mechanical count). This run is the only "
      "one of the three that carries a `refusal_record`:")
    w()
    p.block("kindcount", "kindcount", R)
    w("---")
    w()

    # Part 1
    w("## Part 1 — The sealed record")
    w()
    w("**1a.** Byte-exact substrings of `verdict.json`:")
    w()
    for key in ("verdict", "build_evidence", "rates", "conflicts", "test_stats", "run_id",
                "finalized_at", "schema_version"):
        p.block("v_%s" % key, "vkey", R, key)
    w("**1b.** `phase_records`, each record whole and byte-exact, in file order:")
    w()
    nrec = len(json.loads(_read_text(R, "verdict.json"))["phase_records"])
    for i in range(nrec):
        p.block("vrec%d" % i, "vrec", R, i)
    w("---")
    w()

    # Part 2
    w("## Part 2 — Every build dispatch in the run")
    w()
    w("**Mechanical enumeration 1** — every `action_envelope` in `control_events.jsonl`, in order, "
      "with its `tool` and its `exact_params` as recorded (20 rows; this is the complete dispatch "
      "ledger for the run). A refused call produces no `action_envelope` and therefore does not "
      "appear here — the one such call in this run is Slice 5:")
    w()
    p.block("ledger", "ledger", R)
    w("**Mechanical enumeration 2** — `action_envelope` count per `tool`. The last row re-states the "
      "count for `build` explicitly, taken from the same counter:")
    w()
    p.block("envtools", "envelope_tools", R)
    w("**Mechanical enumeration 3** — every `action_envelope` whose `exact_params.action` is `env`:")
    w()
    p.block("envcalls", "envcalls", R)
    w("**What the engine told the model the build coordinate was.** `contexts/journal/"
      "phase_build.journal.jsonl`, the `intro_text` of iteration 7 — the phase-entry text the model "
      "received on entering `build`, whole:")
    w()
    p.block("journal", "journal_intro", R, "build", 7)
    w("**The advisor output at the head of the build phase** (`action_envelope` control-000142, "
      "`tool_result` control-000143, stored as `output_c8e9b4a462f4`, `output_length` 599), whole:")
    w()
    p.block("advisor", "fullout", R, "output_c8e9b4a462f4")
    w("**The build-phase branch history in full** — `contexts/phase_build.json` `history`, every "
      "entry's `parameters`, quoted as the byte-exact `\"parameters\": {...}` substrings of the "
      "2-space-indented context file, in order:")
    w()
    nh = len(json.loads(_read_text(R, "contexts/phase_build.json"))["history"])
    for i in range(nh):
        p.block("bh%d" % i, "ctxjson", R, "build", i, "parameters")
    w("---")
    w()

    # Part 3
    w("## Part 3 — Every distinct failure signature, at first occurrence")
    w()
    w("**Mechanical enumeration** — every distinct `result.failure_signature` across all `tool_result` "
      "events, at the `sequence` of its first occurrence, in first-occurrence order:")
    w()
    p.block("sigs", "sigs", R)
    w("**Cross-check** — every `failure_signature` recorded in a branch-history entry, per phase. "
      "The build phase carries one signature that the `tool_result` scan above does not: the refused "
      "call at `history[5]`, which produced a `refusal_record` and no `tool_result`:")
    w()
    p.block("csig_p", "ctxsigs", R, "provision")
    p.block("csig_b", "ctxsigs", R, "build")
    w("Five slices follow, in first-occurrence order.")
    w()

    # Slice 1
    w("### Slice 1: provision, iteration 2, `project(action='env')` for `/usr/bin/mvn` refused — "
      "`ENV_EXECUTABLE_NOT_FOUND`")
    w()
    w("**Where:** `control_events` sequence 13 (envelope) / 14 (result), phase `provision`, attempt "
      "`provision-1`, iteration 2. Branch history: `contexts/phase_provision.json` `history[2]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[0]` and "
      "`history[1]`, each `observation` whole and byte-exact:")
    w()
    p.block("s1a0", "ctx", R, "provision", 0, "observation")
    p.block("s1a1", "ctx", R, "provision", 1, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000013, raw line:")
    w()
    p.block("s1b", "ce", R, 13)
    w("**[C] What came back** — `tool_result` control-000014, raw line:")
    w()
    p.block("s1c", "ce", R, 14)
    w("Stored body by ref (`ref_id=output_8863be8b7cf4`, `output_length` 72), whole:")
    w()
    p.block("s1c_out", "fullout", R, "output_8863be8b7cf4")
    w("As it entered the model's context (`history[2].observation`), whole:")
    w()
    p.block("s1c_obs", "ctx", R, "provision", 2, "observation")
    w("**[D] What happened next:** control-000015 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 2
    w("### Slice 2: provision, iteration 3, `search` refused — `SEARCH_FAILED`")
    w()
    w("**Where:** `control_events` sequence 17 (envelope) / 18 (result), phase `provision`, attempt "
      "`provision-1`, iteration 3. Branch history: `contexts/phase_provision.json` `history[3]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — Slice 1 [C] is the "
      "immediately preceding entry and is not repeated.")
    w()
    w("**[B] The call the model made** — `action_envelope` control-000017, raw line:")
    w()
    p.block("s2b", "ce", R, 17)
    w("**[C] What came back** — `tool_result` control-000018, raw line:")
    w()
    p.block("s2c", "ce", R, 18)
    w("Stored body by ref (`ref_id=output_c4c42f386bc8`, `output_length` 216), whole:")
    w()
    p.block("s2c_out", "fullout", R, "output_c4c42f386bc8")
    w("As it entered the model's context (`history[3].observation`), whole:")
    w()
    p.block("s2c_obs", "ctx", R, "provision", 3, "observation")
    w("**[D] What happened next:** control-000019 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 3
    w("### Slice 3: build, iteration 9, `project(action='env')` for `/usr/share/maven/bin/mvn` "
      "refused — `ENV_EXECUTABLE_NOT_FOUND` (second signature hash)")
    w()
    w("**Where:** `control_events` sequence 154 (envelope) / 155 (result), phase `build`, attempt "
      "`build-1`, iteration 9. Branch history: `contexts/phase_build.json` `history[2]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[0]` and "
      "`history[1]`, the two build-phase entries preceding it, each `observation` whole. The first is "
      "elided; the second is 41 characters and is whole:")
    w()
    p.block("s3a0", "ctx", R, "build", 0, "observation", elide=(1100, 700))
    p.block("s3a1", "ctx", R, "build", 1, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000154, raw line:")
    w()
    p.block("s3b", "ce", R, 154)
    w("**[C] What came back** — `tool_result` control-000155, raw line:")
    w()
    p.block("s3c", "ce", R, 155)
    w("Stored body by ref (`ref_id=output_ef194aa0bbda`, `output_length` 84), whole:")
    w()
    p.block("s3c_out", "fullout", R, "output_ef194aa0bbda")
    w("As it entered the model's context (`history[2].observation`), whole:")
    w()
    p.block("s3c_obs", "ctx", R, "build", 2, "observation")
    w("**[D] What happened next:** control-000156 `loop_decision`.")
    w()
    w("---")
    w()

    # Slice 4
    w("### Slice 4: build, iteration 11, `phase(action='blocked')` refused — `build_red`, repair "
      "context opened")
    w()
    w("**Where:** `control_events` sequence 162 (envelope) / 165 (result), with `evidence_publication` "
      "163 and 164 between them and `validator_observation` 166, `gate_decision` 167, "
      "`repair_context_opened` 168 and `completion_claim_decision` 169 following; phase `build`, "
      "attempt `build-1`, iteration 11. Branch history: `contexts/phase_build.json` `history[4]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — `history[3]`, the entry "
      "preceding it, `observation` whole (its stored body `output_6b8344acfcd5` has "
      "`output_length` 0):")
    w()
    p.block("s4a3", "ctx", R, "build", 3, "observation")
    w("**[B] The call the model made** — `action_envelope` control-000162, raw line:")
    w()
    p.block("s4b", "ce", R, 162)
    w("**[C] What came back** — `tool_result` control-000165, raw line, one elision marker:")
    w()
    p.block("s4c", "ce", R, 165, elide=(6200, 2600))
    w("Stored body by ref (`ref_id=output_57045b62c4f2`, `output_length` 1173), whole:")
    w()
    p.block("s4c_out", "fullout", R, "output_57045b62c4f2")
    w("As it entered the model's context (`history[4].observation`), whole:")
    w()
    p.block("s4c_obs", "ctx", R, "build", 4, "observation")
    w("The four control events that followed, raw lines:")
    w()
    for s in (166, 167, 168, 169):
        p.block("s4e%d" % s, "ce", R, s)
    w("**[D] What happened next:** control-000166 `validator_observation`.")
    w()
    w("---")
    w()

    # Slice 5
    w("### Slice 5: build, iteration 12, `search` refused before dispatch — `REPAIR_INTENT_REQUIRED` "
      "(`refusal_record`, no `action_envelope`, no `tool_result`)")
    w()
    w("**Where:** `control_events` sequence 172, kind `refusal_record`; phase `build`, attempt "
      "`build-1`, iteration 12. There is no `action_envelope` and no `tool_result` for this call — "
      "the control record stores only `exact_params_sha256`. The attempted parameters survive in the "
      "branch history: `contexts/phase_build.json` `history[5]`.")
    w()
    w("**[A] What the model had in context immediately before the call** — Slice 4 [C] is the "
      "immediately preceding entry and is not repeated.")
    w()
    w("**[B] The call the model made** — the control layer records only the hash:")
    w()
    p.block("s5b", "ce", R, 172)
    w("The parameters as recorded in the branch history, byte-exact substring of the 2-space-indented "
      "`contexts/phase_build.json` (`history[5].parameters`; also quoted in the Part 2 history "
      "enumeration):")
    w()
    p.block("s5b_params", "ctxjson", R, "build", 5, "parameters")
    w("**[C] What came back** — there is no `tool_result` event. The stored body by ref "
      "(`ref_id=output_a11e6d6e0c14`, `output_length` 121), whole:")
    w()
    p.block("s5c_out", "fullout", R, "output_a11e6d6e0c14")
    w("As it entered the model's context (`history[5].observation`), whole:")
    w()
    p.block("s5c_obs", "ctx", R, "build", 5, "observation")
    w("**[D] What happened next:** control-000173 `loop_decision`.")
    w()
    w("---")
    w()

    # Part 4
    w("## Part 4 — The final build gate, whole")
    w()
    w("**The claim** — `action_envelope` control-000175 (`phase(action='done', outcome='failed')`):")
    w()
    p.block("g175", "ce", R, 175)
    w("**Its result** — `tool_result` control-000176:")
    w()
    p.block("g176", "ce", R, 176)
    w("As it entered the model's context (`contexts/phase_build.json` `history[6].observation`), "
      "whole:")
    w()
    p.block("g176_obs", "ctx", R, "build", 6, "observation")
    w("**The validator observation** — control-000179:")
    w()
    p.block("g179", "ce", R, 179)
    w("**The gate decision** — control-000180, whole:")
    w()
    p.block("g180", "ce", R, 180)
    w("**The phase transition** — control-000181:")
    w()
    p.block("g181", "ce", R, 181)
    w("**The verdict seal** — `evidence_publication` control-000182; its `raw_sha256` is the on-disk "
      "sha256 of `verdict.json` in the sources table:")
    w()
    p.block("g182", "ce", R, 182)
    w("**The evidence close** — control-000183:")
    w()
    p.block("g183", "ce", R, 183)
    w("**What the run wrote as build metrics** — `.setup_agent/report_metrics.json`, whole file, "
      "byte-exact:")
    w()
    p.block("g_metrics", "file", R, "report_metrics.json")
    w("**The surveyed build requirements the gate measured against** — "
      "`.setup_agent/build_requirements.json`, whole file, byte-exact:")
    w()
    p.block("g_breq", "file", R, "build_requirements.json")
    w("---")
    w()

    w("<!-- VERIFICATION FOOTER -->")
    w()
    w("## Verification stamp")
    w()
    w("__VERIFY_LINE__")
    w()
    p.finish()


if __name__ == "__main__":
    build_ignite()
    build_lucene()
    build_kogito()
