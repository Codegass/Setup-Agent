import type { TrajectoryDocument } from "@/api/types"
import type { SparkColumn } from "@/lib/trajectory"
import { formatDuration, formatTokens, seriesPeak, sparkColumns } from "@/lib/trajectory"

/** One column per turn, wide enough to be a target and thin enough to be a spark. */
const COLUMN = 6
const GAP = 2
const PITCH = COLUMN + GAP
const PLOT = 26
const MARKS = 10

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
    label: "tokens per turn",
    value: (column) => column.tokens,
    format: formatTokens,
    unstated: "no tokens stated",
  },
  {
    key: "duration",
    label: "duration per turn",
    value: (column) => column.durationMs,
    format: formatDuration,
    unstated: "no duration stated",
  },
]

/** Exact counts belong in the title a reader hovers, not in a 6px column. */
function exact(series: Series, value: number): string {
  return series.key === "tokens" ? `${value.toLocaleString("en-US")} tokens` : formatDuration(value)
}

function Plot({ columns, series }: { columns: SparkColumn[]; series: Series }) {
  const peak = seriesPeak(columns, series.value)
  const width = Math.max(columns.length * PITCH - GAP, 1)

  return (
    <div className="flex items-end gap-2">
      <svg
        aria-label={series.label}
        className="shrink-0"
        height={PLOT}
        role="img"
        viewBox={`0 0 ${width} ${PLOT}`}
        width={width}
      >
        {columns.map((column, index) => {
          const value = series.value(column)
          const x = index * PITCH
          if (value == null || peak == null) {
            // An absence is drawn as an absence. A zero-height bar would be a
            // turn that cost nothing, which is a different fact.
            return (
              <rect
                className="fill-muted-foreground/35"
                height={1.5}
                key={column.turnId}
                width={COLUMN}
                x={x}
                y={PLOT - 1.5}
              >
                <title>{`turn ${column.turnId} · ${series.unstated}`}</title>
              </rect>
            )
          }
          const height = Math.max(1.5, (value / peak) * PLOT)
          return (
            <rect
              className={INK}
              height={height}
              key={column.turnId}
              rx={1.5}
              width={COLUMN}
              x={x}
              y={PLOT - height}
            >
              <title>{`turn ${column.turnId} · ${exact(series, value)}`}</title>
            </rect>
          )
        })}
      </svg>
      <div className="flex min-w-0 flex-col leading-tight">
        <span className="font-mono text-[10.5px] text-foreground">
          {peak == null ? "—" : series.format(peak)}
        </span>
        <span className="text-[10px] uppercase tracking-[0.06em] text-muted-foreground">
          {`peak ${series.key}`}
        </span>
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
            no turn has been billed yet — the token ledger lands when the loop exits
          </span>
        )}
      </figcaption>

      <div className="overflow-x-auto">
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
                    className="fill-status-failed"
                    height={MARKS - 3}
                    key={column.turnId}
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
            <Plot columns={columns} key={series.key} series={series} />
          ))}
        </div>
      </div>
    </figure>
  )
}
