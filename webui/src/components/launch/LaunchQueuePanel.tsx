import { Rocket } from "lucide-react"

import type { LaunchQueueBatch, LaunchQueueItem, LaunchQueueState } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card, CardHead } from "@/components/common/Card"

interface LaunchQueuePanelProps {
  queue: LaunchQueueState
}

const MAX_FAILED_ROWS = 5

export function LaunchQueuePanel({ queue }: LaunchQueuePanelProps) {
  const { summary } = queue
  const activeBatch = queue.batches.find((batch) => batch.status === "running") ?? null
  const failedItems = queue.batches
    .flatMap((batch) => batch.items.filter((item) => item.status === "failed"))
    .slice(0, MAX_FAILED_ROWS)

  return (
    <Card className="mt-5">
      <CardHead
        icon={<Rocket size={14} className="text-slate-400" />}
        title="Launch queue"
        sub="Web-triggered sag project setups"
        right={
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={summary.queued + summary.launching ? "blue" : "neutral"}>
              {summary.queued + summary.launching} queued
            </Badge>
            <Badge tone={summary.running ? "blue" : "neutral"}>
              {summary.running} running
            </Badge>
            <Badge tone={summary.completed ? "green" : "neutral"}>
              {summary.completed} completed
            </Badge>
            <Badge tone={summary.failed ? "red" : "neutral"}>
              {summary.failed} failed
            </Badge>
          </div>
        }
      />
      <div className="px-4 py-3">
        {activeBatch ? (
          <ActiveBatch batch={activeBatch} />
        ) : (
          <div className="text-[12px] text-slate-400">No batch is currently running.</div>
        )}
        {failedItems.length ? <FailedRows items={failedItems} /> : null}
      </div>
    </Card>
  )
}

function ActiveBatch({ batch }: { batch: LaunchQueueBatch }) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-slate-600">{batch.id}</span>
        <StatusBadge status={batch.status} />
        <span className="font-mono text-[10px] text-slate-400">
          concurrency {batch.concurrency}
        </span>
      </div>
      <div className="mt-2 grid gap-1.5">
        {batch.items.map((item) => (
          <div key={item.id} className="flex min-w-0 items-center gap-2">
            <StatusBadge status={item.status} />
            <span className="truncate font-mono text-[11px] text-slate-600">
              {item.workspace_id}
            </span>
            {item.ref ? (
              <span className="truncate font-mono text-[10px] text-slate-400">
                {item.ref}
              </span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}

function FailedRows({ items }: { items: LaunchQueueItem[] }) {
  return (
    <div className="mt-3 border-t border-slate-100 pt-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
        Recent failures
      </div>
      <div className="mt-1.5 grid gap-2">
        {items.map((item) => (
          <div key={item.id} className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate font-mono text-[11px] text-slate-600">
                {item.workspace_id}
              </span>
              <span className="truncate text-[12px] text-red-600">{item.error}</span>
            </div>
            <div className="truncate font-mono text-[10px] text-slate-400">
              {item.process_log}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
