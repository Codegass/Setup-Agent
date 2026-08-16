import { RefreshCw } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { fetchTrajectory } from "@/api/client"
import type { TrajectoryDocument } from "@/api/types"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import type { BytesStatus } from "@/components/trajectory/RefDescent"
import { TrajectoryTimeline } from "@/components/trajectory/TrajectoryTimeline"
import { TurnSparkline } from "@/components/trajectory/TurnSparkline"
import { DASHBOARD_POLL_MS } from "@/lib/polling"
import { latestControlSeq, mergeTrajectory } from "@/lib/trajectory"

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The Timeline tab: one session's trajectory, live.
 *
 * The whole view is fed by `GET /api/sessions/{id}/trajectory` and nothing
 * else — one derivation serving the timeline, the CLI and the golden fences
 * (spec §1/§4). Two reads, with different jobs:
 *
 * - the SUMMARY tier is what the run is polled at. While the session is live the
 *   poll carries `since_seq=<the last ledger line held>`, so each response
 *   restates every turn the ledger has touched since — the turn that was still
 *   in flight when the last poll caught it as much as the turns that opened
 *   after it — and, per the endpoint's contract, the whole current state of
 *   everything else, which `mergeTrajectory` applies. Cutting at the last TURN
 *   ID instead dropped the in-flight turn from every later response: it stayed
 *   on screen half-stated, its `control_seq` missing the very lines a reader
 *   descends with, and the warnings that named its holes were withdrawn
 *   underneath it by the whole-state rule;
 * - the FULL tier is fetched only when a row is expanded and names bytes the
 *   view does not hold. It is never the polling tier: `outputs` comes back
 *   whole every time.
 *
 * Only the heartbeat is skipped while a read is in flight. A byte read is
 * issued the moment a row is expanded, because a reader is waiting for it, so
 * the two reads DO overlap and a long byte read can answer after a poll that
 * already moved the document on. What it then carries is the run as it stood
 * before that poll, and `mergeTrajectory` orders the two by the same ledger
 * watermark the poll cuts at: an answer behind the watermark already held
 * contributes its bytes and nothing else. It never restores a warning the newer
 * state withdrew, and never returns a turn to the half-stated row it was
 * between its envelope and its result.
 *
 * When the run stops, the timeline reads it whole once more: the token ledger is
 * exported at loop exit, so the last turns' bills land with no control event to
 * carry them, and no watermark cut can ask for a change the ledger never stated.
 * That read WAITS for whatever is on the wire rather than skipping on it — it is
 * not a heartbeat tick that another tick will follow, it is the only read that
 * ever carries those bills, and the interval that would repeat it is gone with
 * the run.
 *
 * "Reload whole" is the owner's read, and it is the one read the watermark does
 * not order: a person asking for the server's whole current answer is not one of
 * the two reads that race over this document.
 */
export function TimelineTab({ sessionId, live }: { sessionId: string; live: boolean }) {
  const [doc, setDoc] = useState<TrajectoryDocument | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [bytes, setBytes] = useState<BytesStatus>("absent")
  const [bytesError, setBytesError] = useState<string | null>(null)

  // The held document is a ref as well as state: the poll interval reads it to
  // choose its `since=` cut, and re-creating the interval on every new turn
  // would restart the clock on every poll.
  const held = useRef<TrajectoryDocument | null>(null)
  const resolvedThrough = useRef<number | null>(null)
  const bytesStatus = useRef<BytesStatus>("absent")
  // A read of a long ledger can outlast the heartbeat. A poll that fired anyway
  // put one request per tick on the wire and let an older answer land after a
  // newer one — a document going backwards. The heartbeat skips instead; a read
  // the OWNER asked for is never skipped, because they are waiting for it.
  const reading = useRef(false)
  // The read on the wire, so a caller that was skipped can be told WHEN it may
  // ask again instead of only that it was skipped.
  const inFlight = useRef<Promise<unknown> | null>(null)

  const apply = useCallback((incoming: TrajectoryDocument, options?: { force?: boolean }) => {
    const merged = mergeTrajectory(held.current, incoming, options)
    held.current = merged
    setDoc(merged)
  }, [])

  /** Read the trajectory. Resolves `true` when it read, `false` when it was
   *  skipped — and a skipped call resolves only once the read it stood down
   *  for has settled, so a caller that must not be dropped can ask again. */
  const load = useCallback(
    (options?: { whole?: boolean; silent?: boolean; force?: boolean }): Promise<boolean> => {
      if (options?.silent && reading.current) {
        return (inFlight.current ?? Promise.resolve()).then(() => false)
      }
      reading.current = true
      if (!options?.silent) {
        setLoading(true)
      }
      const read = async () => {
        try {
          const sinceSeq = options?.whole || !held.current ? null : latestControlSeq(held.current)
          const incoming = await fetchTrajectory(
            sessionId,
            sinceSeq == null ? undefined : { sinceSeq },
          )
          apply(incoming, { force: options?.force })
          setError(null)
        } catch (err) {
          setError(message(err))
        } finally {
          reading.current = false
          inFlight.current = null
          if (!options?.silent) {
            setLoading(false)
          }
        }
        return true
      }
      const running = read()
      inFlight.current = running
      return running
    },
    [apply, sessionId],
  )

  // A new session is a new document; nothing of the old one survives the swap.
  useEffect(() => {
    held.current = null
    resolvedThrough.current = null
    bytesStatus.current = "absent"
    setDoc(null)
    setBytes("absent")
    setBytesError(null)
    void load()
  }, [load])

  useEffect(() => {
    if (!live) {
      return
    }
    const interval = window.setInterval(() => {
      void load({ silent: true })
    }, DASHBOARD_POLL_MS)
    return () => window.clearInterval(interval)
  }, [live, load])

  // The run stopping is itself an event to read on: the token ledger is written
  // at loop exit, so the bills for the last turns exist only in a whole read.
  // A poll on the wire at that moment makes this read stand down like any other
  // silent one — and nothing would ever issue it again, because the interval
  // that repeats a poll is cleared with the run. So it asks again as soon as
  // the read it stood down for has settled, and keeps asking until it reads.
  const wasLive = useRef(live)
  useEffect(() => {
    const stopped = wasLive.current && !live
    wasLive.current = live
    if (!stopped) {
      return
    }
    let abandoned = false
    const readWhole = async () => {
      let read = await load({ whole: true, silent: true })
      while (!read && !abandoned) {
        read = await load({ whole: true, silent: true })
      }
    }
    void readWhole()
    return () => {
      abandoned = true
    }
  }, [live, load])

  const needBytes = useCallback(() => {
    const held_seq = held.current ? latestControlSeq(held.current) : null
    if (bytesStatus.current === "loading") {
      return
    }
    // Already resolved through this ledger line: whatever the row still cannot
    // show is a ref no store in this session answers, and asking again would not
    // change that. The watermark is the ledger's, not the turn list's — a turn
    // already held names its observation only once the result that answered it
    // lands, so a guard reading turn ids would refuse to fetch the bytes of
    // every ref that arrived on a turn the view was already showing.
    if (
      bytesStatus.current === "ready" &&
      resolvedThrough.current != null &&
      (held_seq == null || held_seq <= resolvedThrough.current)
    ) {
      return
    }

    bytesStatus.current = "loading"
    setBytes("loading")
    setBytesError(null)
    // This read is NOT skipped while a poll is in flight — the reader is
    // waiting for these bytes — so it may answer after a newer poll has landed.
    // `apply` refuses to let an answer behind the held watermark overwrite the
    // newer state; the watermark it resolved through is recorded as its OWN, so
    // a stale answer leaves the bytes of the turns that arrived after it still
    // to fetch, and the next expansion asks again.
    void fetchTrajectory(sessionId, { detail: "full" })
      .then((full) => {
        apply(full)
        bytesStatus.current = "ready"
        resolvedThrough.current = latestControlSeq(full)
        setBytes("ready")
      })
      .catch((err) => {
        bytesStatus.current = "error"
        setBytes("error")
        setBytesError(message(err))
      })
  }, [apply, sessionId])

  if (loading && !doc) {
    return (
      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        Reading the control ledger…
      </div>
    )
  }

  if (!doc) {
    return (
      <Card className="max-w-xl p-5">
        <div className="text-[15px] font-semibold text-foreground">Trajectory unavailable</div>
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
            {`${doc.warnings.length} hole${doc.warnings.length === 1 ? "" : "s"} stated`}
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

      <TrajectoryTimeline doc={doc} onNeedBytes={needBytes} status={bytes} />
    </div>
  )
}
