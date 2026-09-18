import { Fragment, useState } from "react"

import type { TrajectoryDocument } from "@/api/types"
import type { SparkColumn } from "@/lib/trajectory"
import { formatDuration, formatTokens, sparkColumns } from "@/lib/trajectory"
import { cn } from "@/lib/utils"

/** One column per turn, wide enough to be a target and thin enough to be a spark. */
const COLUMN = 6
const GAP = 2
const PITCH = COLUMN + GAP
const PLOT = 26
const MARKS = 10
/** The shortest a column is ever drawn: a value stated, whatever its size. */
const BASELINE = 1.5

/**
 * What a series is: one measure, one scale, one label, one colour.
 *
 * Tokens and seconds are different units, so they are never drawn against one
 * shared axis — two scales on one plot is the chart that lies most often. They
 * are two plots on the same columns instead, each scaled to its own peak, each
 * naming that peak, and aligned turn-for-turn because they share this geometry.
 */
interface Series {
  key: string
  label: string
  detail: string
  /** The value this series reads off a column, or null when none was stated. */
  value: (column: SparkColumn) => number | null
  /** How a value is spoken, in the axis label and in a column's own title. */
  format: (value: number) => string
  /** What one column is called when nothing was stated for it. */
  unstated: string
}

/**
 * One ink for data, and the status palette left alone.
 *
 * These are two measures, not two series: each plot holds one series and names
 * it, so colour carries no identity here and a second hue would only invite the
 * reader to look for a meaning it does not have. The status colours the app
 * reserves stay reserved — `status-failed` marks an anomaly, which is a state,
 * and it never doubles as "the other measure".
 */
const INK = "fill-primary"

const SERIES: Series[] = [
  {
    key: "tokens",
    label: "Model-response tokens per turn",
    detail: "Input plus output tokens attributed to this turn",
    value: (column) => column.tokens,
    format: formatTokens,
    unstated: "no tokens stated",
  },
  {
    key: "duration",
    label: "Action duration per turn",
    detail: "Elapsed wall time recorded for this turn",
    value: (column) => column.durationMs,
    format: formatDuration,
    unstated: "no duration stated",
  },
]

/** Exact counts belong in the title a reader hovers, not in a 6px column. */
function exact(series: Series, value: number): string {
  return series.key === "tokens" ? `${value.toLocaleString("en-US")} tokens` : formatDuration(value)
}

function peakColumn(
  columns: SparkColumn[],
  pick: (column: SparkColumn) => number | null,
): { column: SparkColumn; value: number } | null {
  let peak: { column: SparkColumn; value: number } | null = null
  for (const column of columns) {
    const value = pick(column)
    if (value != null && (peak === null || value > peak.value)) {
      peak = { column, value }
    }
  }
  return peak
}

function Plot({
  columns,
  series,
  hoveredTurn,
  onHoverTurn,
}: {
  columns: SparkColumn[]
  series: Series
  /** The turn the reader is pointing at, in either plot. Both plots draw the
   *  same columns, so pointing at one turn names it in both. */
  hoveredTurn: number | null
  onHoverTurn: (turnId: number | null) => void
}) {
  const peak = peakColumn(columns, series.value)
  const peakValue = peak?.value ?? null
  const width = Math.max(columns.length * PITCH - GAP, 1)
  const hovered = hoveredTurn == null ? null : columns.find((c) => c.turnId === hoveredTurn) ?? null
  const hoveredValue = hovered ? series.value(hovered) : null

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-[11px] font-semibold text-foreground">{series.label}</span>
        <span className="text-[10.5px] text-muted-foreground">{series.detail}</span>
      </div>
      <div className="flex items-end gap-2">
        <svg
          aria-label={series.label}
          className="shrink-0"
          height={PLOT}
          onMouseLeave={() => onHoverTurn(null)}
          role="img"
          viewBox={`0 0 ${width} ${PLOT}`}
          width={width}
        >
          {columns.map((column, index) => {
            const value = series.value(column)
            const x = index * PITCH
            const stated = value != null && peakValue != null
            // Pointing at one column is a claim about that column, so the rest
            // step back rather than the pointed-at one shouting over them.
            const faded = hoveredTurn != null && hoveredTurn !== column.turnId
            // An absence is drawn as an absence. A zero-height bar would be a
            // turn that cost nothing, which is a different fact. A peak of zero
            // is a series every column of which stated zero, and a stated value
            // keeps a visible baseline whatever its size.
            const height = stated
              ? peakValue > 0
                ? Math.max(BASELINE, (value / peakValue) * PLOT)
                : BASELINE
              : BASELINE
            return (
              // A column is one thing: the bar that states its value and the
              // area a reader can point at to ask about it. The bar is the wrong
              // target on its own — a turn that cost almost nothing is drawn
              // 1.5px tall, and the 2px between columns catches nothing at all —
              // so the group carries a full-height target a whole pitch wide.
              // Targets cannot overlap a neighbour's bar: this one ends exactly
              // where the next column begins.
              <Fragment key={column.turnId}>
                <rect
                  className={cn(
                    stated ? INK : "fill-muted-foreground/35",
                    faded && "opacity-30",
                  )}
                  data-bar={column.turnId}
                  height={height}
                  rx={stated ? 1.5 : undefined}
                  width={COLUMN}
                  x={x}
                  y={PLOT - height}
                />
                <rect
                  data-hit={column.turnId}
                  fill="transparent"
                  height={PLOT}
                  onMouseEnter={() => onHoverTurn(column.turnId)}
                  width={PITCH}
                  x={x}
                  y={0}
                >
                  <title>
                    {`turn ${column.turnId} · ${stated ? exact(series, value) : series.unstated}`}
                  </title>
                </rect>
              </Fragment>
            )
          })}
        </svg>
        {/* The readout already had the shape a hover wants — a value and the
            turn it belongs to — so pointing at a column answers here rather
            than in a floating tooltip that would have to be positioned, sized
            and kept out of its own way. Let go and it returns to the peak. */}
        <div className="flex min-w-0 flex-col leading-tight">
          <span className="font-mono text-[10.5px] text-foreground">
            {hovered
              ? hoveredValue == null
                ? "—"
                : exact(series, hoveredValue)
              : peakValue == null
                ? "—"
                : series.format(peakValue)}
          </span>
          <span className="whitespace-nowrap text-[10px] text-muted-foreground">
            {hovered
              ? `Turn ${hovered.turnId}${
                  hoveredValue == null
                    ? ` · ${series.unstated}`
                    : hovered.tool
                      ? ` · ${hovered.tool}`
                      : ""
                }`
              : peak
                ? `Max at Turn ${peak.column.turnId}${peak.column.tool ? ` · ${peak.column.tool}` : ""}`
                : `No ${series.key} stated`}
          </span>
        </div>
      </div>
    </div>
  )
}

/**
 * The run's shape at a glance: what each turn cost, and where it bled.
 *
 * Everything drawn here was stated by the trajectory reducer at the SUMMARY
 * tier — the tier a live run is polled at — so the header keeps up with the
 * timeline underneath it without ever asking for bytes. Three strips over one
 * set of columns: the marks the reducer drew, the tokens each turn was billed,
 * and the wall time it took. Nothing is interpolated, smoothed, or filled in;
 * a turn the ledger has not billed yet is a gap, and says so on hover.
 */
export function TurnSparkline({ doc }: { doc: TrajectoryDocument }) {
  // Held here rather than in each plot: the strips are drawn on one set of
  // columns and the docstring above promises they are aligned turn-for-turn, so
  // pointing at a turn should name that turn everywhere it appears.
  const [hoveredTurn, setHoveredTurn] = useState<number | null>(null)
  const columns = sparkColumns(doc)
  if (!columns.length) {
    return null
  }

  const marked = columns.filter((column) => column.anomalies.length)
  const totalMarks = marked.reduce((sum, column) => sum + column.anomalies.length, 0)
  const width = Math.max(columns.length * PITCH - GAP, 1)
  const billed = columns.some((column) => column.tokens != null)

  return (
    <figure className="rounded-lg border border-border bg-card px-3 py-2">
      <figcaption className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
          per turn
        </span>
        <span className="font-mono text-[10.5px] text-muted-foreground">
          {`${columns.length} turn${columns.length === 1 ? "" : "s"}`}
        </span>
        {totalMarks ? (
          <span className="font-mono text-[10.5px] text-status-failed">
            {`${totalMarks} anomaly mark${totalMarks === 1 ? "" : "s"}`}
          </span>
        ) : null}
        {billed ? null : (
          <span className="text-[10.5px] text-muted-foreground">
            no turn has been billed yet; the token ledger lands when the loop exits
          </span>
        )}
      </figcaption>

      <div className="overflow-x-auto" onMouseLeave={() => setHoveredTurn(null)}>
        <div className="flex flex-col gap-1" style={{ minWidth: width }}>
          {totalMarks ? (
            <svg
              aria-label={`${totalMarks} anomaly mark${totalMarks === 1 ? "" : "s"}`}
              height={MARKS}
              role="img"
              viewBox={`0 0 ${width} ${MARKS}`}
              width={width}
            >
              {columns.map((column, index) =>
                column.anomalies.length ? (
                  <rect
                    className={cn(
                      "fill-status-failed",
                      hoveredTurn != null && hoveredTurn !== column.turnId && "opacity-30",
                    )}
                    height={MARKS - 3}
                    key={column.turnId}
                    onMouseEnter={() => setHoveredTurn(column.turnId)}
                    rx={1.5}
                    width={COLUMN}
                    x={index * PITCH}
                    y={0}
                  >
                    <title>
                      {`turn ${column.turnId} · ${column.anomalies
                        .map((mark) => mark.label)
                        .join(" · ")} — ${column.anomalies
                        .map((mark) => mark.detail)
                        .join("; ")}`}
                    </title>
                  </rect>
                ) : null,
              )}
            </svg>
          ) : null}

          {SERIES.map((series) => (
            <Plot
              columns={columns}
              hoveredTurn={hoveredTurn}
              key={series.key}
              onHoverTurn={setHoveredTurn}
              series={series}
            />
          ))}
        </div>
      </div>
      <p className="mt-2 text-[10.5px] leading-relaxed text-muted-foreground">
        Blue columns are stated values. Gray ticks mean no value is attributed to that turn.
        Tokens are recorded once per model response; duration is elapsed time for the turn.
      </p>
    </figure>
  )
}
