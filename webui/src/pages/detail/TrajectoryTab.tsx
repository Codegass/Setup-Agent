import { RefreshCw } from "lucide-react"

import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { TrajectoryTimeline } from "@/components/trajectory/TrajectoryTimeline"
import { TurnSparkline } from "@/components/trajectory/TurnSparkline"
import { useTrajectory } from "./useTrajectory"

export function TrajectoryTab({ sessionId, live }: { sessionId: string; live: boolean }) {
  const { doc, error, loading, bytes, bytesError, load, needBytes } = useTrajectory(sessionId, live)

  if (loading && !doc) {
    return (
      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        Reading the run's record…
      </div>
    )
  }

  if (!doc) {
    return (
      <Card className="max-w-xl p-5">
        <div className="text-[15px] font-semibold text-foreground">Turns unavailable</div>
        <div className="mt-2 font-mono text-[12px] text-status-failed">{error}</div>
        <Button className="mt-4" onClick={() => void load()} type="button" variant="outline">
          Retry
        </Button>
      </Card>
    )
  }

  const turns = doc.turns.length
  const runWide = doc.warnings.filter((warning) => warning.turn_id == null)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="font-mono text-[12px] font-semibold text-foreground">
          {doc.session.project ?? doc.session.run_id}
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">{doc.session.run_id}</span>
        <span className="text-[11.5px] text-muted-foreground">
          {`${turns} turn${turns === 1 ? "" : "s"}`}
        </span>
        {doc.session.verdict ? (
          <span className="rounded-full border border-border bg-muted px-2 py-0.5 font-mono text-[10.5px] text-muted-foreground">
            {doc.session.verdict}
          </span>
        ) : null}
        {doc.warnings.length ? (
          <span className="rounded-full border border-status-attention-border bg-status-attention-soft px-2 py-0.5 text-[10.5px] text-status-attention">
            {`${doc.warnings.length} gap${doc.warnings.length === 1 ? "" : "s"} in the record`}
          </span>
        ) : null}
        {/* The pill is a claim about freshness. A green "following" over a poll
            that last failed would say the rows are current when they are the
            run as it stood; the follow keeps trying, and says which it is. */}
        {live && !error ? (
          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-status-running">
            <span className="h-1.5 w-1.5 rounded-full bg-status-running" />
            following
          </span>
        ) : null}
        {live && error ? (
          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-status-attention">
            <span className="h-1.5 w-1.5 rounded-full bg-status-attention" />
            retrying
          </span>
        ) : null}
        {/* The one read the watermark does not order: the owner asked for the
            server's whole current answer, and gets it whatever is held. */}
        <Button
          className="ml-auto"
          onClick={() => void load({ whole: true, force: true })}
          type="button"
          variant="outline"
        >
          <RefreshCw size={13} />
          Reload whole
        </Button>
      </div>

      {error ? (
        <div className="rounded-lg border border-status-failed-border bg-status-failed-soft px-3 py-2 font-mono text-[11.5px] text-status-failed">
          {`The last read failed: ${error}. The rows below are the run as it stood.`}
        </div>
      ) : null}

      {bytesError ? (
        <div className="rounded-lg border border-status-failed-border bg-status-failed-soft px-3 py-2 font-mono text-[11.5px] text-status-failed">
          {`The bytes could not be fetched: ${bytesError}`}
        </div>
      ) : null}

      <TurnSparkline doc={doc} />

      {runWide.length ? (
        <ul className="space-y-1 rounded-lg border border-status-attention-border bg-status-attention-soft/40 px-3 py-2">
          {runWide.map((warning, index) => (
            <li className="flex flex-wrap items-baseline gap-2" key={`${warning.code}-${index}`}>
              <span className="font-mono text-[11px] font-semibold text-status-attention">
                {warning.code}
              </span>
              <span className="text-[11.5px] text-muted-foreground">{warning.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <TrajectoryTimeline
        doc={doc}
        onNeedBytes={needBytes}
        sessionId={sessionId}
        status={bytes}
      />
    </div>
  )
}
