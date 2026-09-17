import type { Attainment, CIComparison, LifecycleParity, Pair } from "@/api/types"
import { gloss } from "@/lib/ciGlosses"
import { cn } from "@/lib/utils"

/** How many failing test names to print before summarising the rest. */
const MAX_RED_IDS = 25

/** Where the module list CI was measured against came from. */
const MODULES_BASIS: Record<string, string> = {
  log: "the CI job log",
  declared: "the modules the project declares",
  test_bearing: "the modules that carry tests",
}

/** What the parity record says about the two command lists. The record's own
 *  three words, said as sentences: "unknown" is 216 of the 264 real payloads
 *  that carry a parity block, and on a screen the bare word reads as a verdict
 *  about the commands rather than about the comparison. */
const PARITY_SENTENCE: Record<string, string> = {
  equivalent: "The two commands cover the same steps",
  not_equivalent: "The two commands do not cover the same steps",
  unknown: "Whether the two commands cover the same steps was not worked out",
}

const VERDICT_CHIP: Record<string, string> = {
  met: "bg-status-success-soft text-status-success",
  exceeded: "bg-status-success-soft text-status-success",
  partial: "bg-status-attention-soft text-status-attention",
  not_met: "bg-status-failed-soft text-status-failed",
  invalid: "bg-status-failed-soft text-status-failed",
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`
}

function Section({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <section aria-label={title} className="mb-6">
      <h3 className="mb-2 text-[12px] font-bold uppercase tracking-[0.06em] text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  )
}

/** A value the run did not record. Said out loud, never left blank — a blank
 *  row reads as a zero, and none of these are zeroes. */
function NotRecorded() {
  return <span className="text-[13px] italic text-muted-foreground">Not recorded</span>
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1">
      <dt className="w-32 shrink-0 text-[12.5px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1 text-[13px] text-foreground">{children}</dd>
    </div>
  )
}

function Mono({ children, title }: { children: string; title?: string }) {
  return (
    <code className="break-all font-mono text-[12px] text-foreground" title={title}>
      {children}
    </code>
  )
}

function Line({ children }: { children: React.ReactNode }) {
  return <p className="py-0.5 text-[13px] text-foreground">{children}</p>
}

/** What was compared. Every row is stated, including the ones with nothing in
 *  them: of the 681 runs recorded under `logs/`, 108 have no commit and 155 no
 *  matched CI job, and a row that vanished would read as a row that passed. */
function Target({ comparison }: { comparison: CIComparison }) {
  const sha = comparison.target_sha
  const result = comparison.attainment
  return (
    <Section title="What was compared">
      <dl>
        <Field label="Repository">
          {comparison.repo ? <Mono>{comparison.repo}</Mono> : <NotRecorded />}
        </Field>
        <Field label="Commit">
          {sha ? <Mono title={sha}>{sha.slice(0, 7)}</Mono> : <NotRecorded />}
        </Field>
        <Field label="CI job">
          {result ? `${result.cell_id} · grade ${result.cell_grade}` : <NotRecorded />}
        </Field>
        {/* `acceptance_command` is the command derived from the CI job's and
            pinned for this run to match; the CI job's own command is in
            Commands below, and on 161 of 435 real payloads the two differ. */}
        <Field label="Command this run had to match">
          {comparison.acceptance_command ? (
            <Mono>{comparison.acceptance_command}</Mono>
          ) : (
            <NotRecorded />
          )}
        </Field>
      </dl>
    </Section>
  )
}

/** One fraction, or the statement that it is not there.
 *
 *  Each of the three is absent on its own terms — over the 342 real evaluated
 *  comparisons the overall score and the test fraction are missing on 171 runs
 *  and the module fraction on 137 — so each says so itself, and none of them
 *  offers a reason. The reasons the payload did state are in Findings below;
 *  guessing at one it did not is how a surface starts lying.
 */
function Fraction({
  pair,
  present,
  absent,
}: {
  pair?: Pair | null
  present: (pair: Pair) => string
  absent: string
}) {
  if (!pair) {
    return <Line>{absent}</Line>
  }
  return <Line>{present(pair)}</Line>
}

function Measurement({ result }: { result: Attainment }) {
  const basis = result.modules_basis ? MODULES_BASIS[result.modules_basis] : null
  return (
    <Section title="How this run measured against the CI job">
      <p className="mb-2">
        <span
          className={cn(
            "inline-block rounded-full px-2.5 py-0.5 text-[12px] font-semibold",
            VERDICT_CHIP[result.verdict] ?? "bg-accent text-muted-foreground",
          )}
        >
          {result.verdict.replace(/_/g, " ")}
        </span>
      </p>
      <Fraction
        absent="No overall score"
        pair={result.alpha}
        present={(p) => `Overall ${p.numerator}/${p.denominator}`}
      />
      <Fraction
        absent="No test count was compared"
        pair={result.alpha_test}
        present={(p) => `${p.numerator} of CI's ${plural(p.denominator, "test")} ran here`}
      />
      <Fraction
        absent="No module count was compared"
        pair={result.alpha_build}
        present={(p) =>
          `${p.numerator} of CI's ${plural(p.denominator, "module")} matched` +
          (basis ? ` · counted from ${basis}` : "")
        }
      />
      {result.missing_module_ids.length > 0 ? (
        <Line>
          <span className="text-muted-foreground">CI built and this run did not: </span>
          {result.missing_module_ids.join(", ")}
        </Line>
      ) : null}
    </Section>
  )
}

/** The CI job's command beside the run's own. `lifecycle_parity` is absent on
 *  85 of the real runs, which is stated rather than shown as an empty list. */
function Commands({ parity }: { parity?: LifecycleParity | null }) {
  if (!parity) {
    return (
      <Section title="Commands">
        <Line>No commands were compared</Line>
      </Section>
    )
  }
  return (
    <Section title="Commands">
      <Line>{PARITY_SENTENCE[parity.status] ?? parity.status.replace(/_/g, " ")}</Line>
      <div className="mt-1.5 space-y-1.5">
        <div>
          <p className="text-[12px] text-muted-foreground">CI ran</p>
          <Mono>{parity.ci_command}</Mono>
        </div>
        <div>
          <p className="text-[12px] text-muted-foreground">This run ran</p>
          {parity.sag_commands.length === 0 ? (
            <NotRecorded />
          ) : (
            <ul className="space-y-0.5">
              {parity.sag_commands.map((command) => (
                <li key={command}>
                  <Mono>{command}</Mono>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {parity.missing.length > 0 ? (
        <Line>
          <span className="text-muted-foreground">CI reached these and this run did not: </span>
          {parity.missing.join(", ")}
        </Line>
      ) : null}
      {parity.extra.length > 0 ? (
        <Line>
          <span className="text-muted-foreground">This run reached these and CI did not: </span>
          {parity.extra.join(", ")}
        </Line>
      ) : null}
    </Section>
  )
}

/** The section title says what these are; the list says which. The count is
 *  the list's own length, and `NEW_RED_BEYOND_TARGET` in Findings already
 *  carries the sentence, so neither is repeated here. */
function RedTests({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null
  const shown = ids.slice(0, MAX_RED_IDS)
  return (
    <Section title="Tests that failed here and pass in CI">
      <ul className="mt-1 space-y-0.5">
        {shown.map((id) => (
          <li className="font-mono text-[12px] text-foreground" key={id}>
            {id}
          </li>
        ))}
      </ul>
      {ids.length > shown.length ? (
        <p className="mt-1 text-[12px] text-muted-foreground">
          {`and ${ids.length - shown.length} more`}
        </p>
      ) : null}
    </Section>
  )
}

/**
 * Everything the comparison recorded about itself, in the order it recorded it.
 *
 * An evaluated comparison's own `reasons` list is a superset of the
 * attainment's `reason_codes` — 127 of the 342 real evaluated comparisons carry
 * statements that appear only there, most of them the reasons a certificate was
 * blocked — so both are read and the union is shown once.
 */
function Findings({ comparison }: { comparison: CIComparison }) {
  const codes: string[] = []
  for (const code of [...(comparison.attainment?.reason_codes ?? []), ...comparison.reasons]) {
    if (!codes.includes(code)) codes.push(code)
  }
  if (codes.length === 0) return null
  return (
    <Section title="Findings">
      <ul className="space-y-1">
        {codes.map((code) => {
          const sentence = gloss(code)
          return (
            <li className="flex flex-wrap items-baseline gap-x-2" key={code}>
              <code className="font-mono text-[12px] text-muted-foreground">{code}</code>
              {sentence === code ? null : (
                <span className="text-[13px] text-foreground">{sentence}</span>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

/**
 * How this run measured against the project's own CI on the same commit.
 *
 * Every number on this panel is a fraction the comparison recorded, shown with
 * the denominator it was measured against. A fraction the comparison did not
 * produce is stated as absent and given no explanation here — the payload's own
 * reason codes, in Findings, are the only account of why, and when the payload
 * gave none this panel gives none either.
 */
export function OfficialCITab({ comparison }: { comparison: CIComparison }) {
  const result = comparison.attainment
  const compared = comparison.status === "evaluated" && result != null
  return (
    <div>
      <Target comparison={comparison} />
      {compared ? (
        <>
          <Measurement result={result} />
          <Commands parity={result.lifecycle_parity} />
          <RedTests ids={result.unexpected_red_ids} />
        </>
      ) : (
        <Section title="Result">
          <Line>
            Official CI was not compared
            {comparison.reasons.length > 0 ? ". The findings below say what the run recorded." : "."}
          </Line>
        </Section>
      )}
      <Findings comparison={comparison} />
    </div>
  )
}
