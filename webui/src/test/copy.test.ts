/** The Workbench's words, checked in both languages that produce them.
 *
 *  A run's screens are written in two places. Most copy is a string literal in
 *  `webui/src`; some of it — the evidence group headings, the build facet's
 *  Tool and Command values — is composed in Python and arrives in the payload,
 *  already spelled, for the browser to print verbatim. A lint that reads only
 *  TSX cannot see the second half, so this one reads `src/sag/web` as well.
 *  The Python leg is `tests/test_workbench_copy.py`, which walks the module's
 *  syntax tree (far more accurate than a regex over Python) and pins its word
 *  list against the one below, so the two legs cannot drift apart.
 */

import { readFileSync, readdirSync, statSync } from "node:fs"
import { join, resolve } from "node:path"

import { describe, expect, it } from "vitest"

const WEBUI = resolve(__dirname, "..", "..")

/** Retired vocabulary. These are house words for machinery a reader of the
 *  Workbench has no reason to know about; every one of them once shipped to a
 *  screen. `constraints.md` is the source of truth for this list, and
 *  `tests/test_workbench_copy.py` asserts the Python leg forbids the same nine. */
export const FORBIDDEN = [
  "sealed",
  "canonical",
  "claimed",
  "quarantined",
  "subject",
  "snapshot",
  "metrics-v2",
  "verdict-bearing",
  "promoting",
]

/** A raw reason code is a machine handle, not prose. `UNSEALED_DENOMINATOR`
 *  beside its plain-English gloss is what makes an issue reportable, so the
 *  code itself is exempt: strip the SCREAMING_SNAKE tokens before matching. */
const RAW_CODE = /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g

/** Text a person reads: JSX text nodes, and the string literals in the props
 *  that become text. Identifiers and field names are not copy — which is why
 *  `${...}` interpolations are blanked first: `${subjectCounts.reason}` inside
 *  a template literal is a field path, and reading it as copy flagged three
 *  innocent lines in `WorkspaceRail.tsx`. */
function visibleText(source: string): string[] {
  const text = source.replace(/\$\{[^}]*\}/g, " ")
  const found: string[] = []
  for (const match of text.matchAll(/>\s*([A-Za-z][^<>{}]{3,})\s*</g)) found.push(match[1])
  for (const match of text.matchAll(/(?:label|title|aria-label|placeholder)=["']([^"']+)["']/g))
    found.push(match[1])
  for (const match of text.matchAll(/["'`]([A-Z][a-z][^"'`]{6,})["'`]/g)) found.push(match[1])
  return found
}

export function offendingWord(text: string): string | null {
  const lowered = text.replace(RAW_CODE, " ").toLowerCase()
  return FORBIDDEN.find((word) => lowered.includes(word)) ?? null
}

function sourceFiles(): string[] {
  const found: string[] = []
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry)
      if (statSync(path).isDirectory()) {
        walk(path)
        continue
      }
      if (!/\.tsx?$/.test(entry)) continue
      if (/\.test\.tsx?$/.test(entry)) continue
      if (path.endsWith(join("api", "types.ts"))) continue
      // The gloss table is a verbatim mirror of `src/sag/result_card/glosses.py`,
      // fenced by `tests/test_gloss_parity.py`; its codes are machine handles.
      if (path.endsWith(join("lib", "ciGlosses.ts"))) continue
      found.push(path)
    }
  }
  walk(join(WEBUI, "src"))
  return found
}

describe("user-visible copy", () => {
  it("never shows the retired vocabulary", () => {
    const files = sourceFiles()
    expect(files.length).toBeGreaterThan(40)
    const offenders: string[] = []
    for (const file of files) {
      const source = readFileSync(file, "utf8")
      for (const text of visibleText(source)) {
        const word = offendingWord(text)
        if (word) offenders.push(`${file.slice(WEBUI.length + 1)} says ${word}: ${text.trim()}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it("exempts a raw reason code but not the sentence beside it", () => {
    expect(offendingWord("UNSEALED_DENOMINATOR")).toBeNull()
    expect(offendingWord("COMPARISON_SUBJECT_MISMATCH: the two runs covered different tests")).toBeNull()
    expect(offendingWord("UNSEALED_DENOMINATOR: the sealed total is unknown")).toBe("sealed")
  })

  it("has no dead components left in the tree", () => {
    const names = sourceFiles().map((path) => path.replace(/^.*\//, "").replace(/\.tsx?$/, ""))
    for (const name of [
      "SummaryStrip",
      "BuildCard",
      "TestCard",
      "FacetTabs",
      "ContextTrace",
      "FlowTab",
      "ActionDetailModal",
      "FilesDigest",
      "VerdictBand",
      "scrollSpy",
    ]) {
      expect(names).not.toContain(name)
    }
  })
})
