/**
 * How long a run took, in the words the result card's Setup row uses.
 *
 * This is `duration_text` in `src/sag/result_card/rows.py`, rule for rule:
 * under a minute keeps a decimal, minutes pad their seconds to two digits, and
 * past an hour the seconds are dropped. The card carries `wallClockSeconds` as
 * a number and every surface that prints it has to reach the same string, or
 * the header line and the row an inch below it state two lengths for one run —
 * which is what they did before this existed.
 *
 * Answers null for a length the run did not measure, so a caller has to decide
 * what to say about that rather than being handed a zero.
 */
export function durationText(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
    return null
  }
  const total = Math.round(seconds)
  if (total < 60) {
    return `${seconds.toFixed(1)}s`
  }
  const pad = (value: number) => String(value).padStart(2, "0")
  if (total < 3600) {
    return `${Math.floor(total / 60)}m ${pad(total % 60)}s`
  }
  return `${Math.floor(total / 3600)}h ${pad(Math.floor((total % 3600) / 60))}m`
}
