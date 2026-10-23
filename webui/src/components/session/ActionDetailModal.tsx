import { useState } from "react"

import type { ContextReference, ContextTrace } from "@/api/types"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

type PhaseAction =
  ContextTrace["phases"][number]["tasks"][number]["iterations"][number]["actions"][number]
type ContextRef = ContextReference | string

/** The honest status the modal reports — pending dispatch shows running, not a
 *  premature success/failure. */
function statusMeta(action: PhaseAction): { label: string; tone: "success" | "running" | "failed" | "neutral" } {
  if (action.dispatchStatus === "pending") return { label: "running", tone: "running" }
  if (action.success === true) return { label: "success", tone: "success" }
  if (action.success === false) return { label: "failed", tone: "failed" }
  return { label: "not recorded", tone: "neutral" }
}

/** A bounded context may carry only a ref. Keep it visible so the reader can
 *  request its bytes from the same archive resolver used by Turns. */
function outputRefs(refs: ContextRef[]): ContextReference[] {
  const unique = new Map<string, ContextReference>()
  for (const item of refs ?? []) {
    const ref = typeof item === "string" ? { ref: item, label: item } : item
    if (!unique.has(ref.ref) || ref.content != null) unique.set(ref.ref, ref)
  }
  return [...unique.values()]
}

function SectionLabel({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {title}
      </span>
      <span className="text-[11px] text-muted-foreground">{hint}</span>
    </div>
  )
}

export function ActionDetailModal({
  action,
  onClose,
  onRestoreFocus,
  loadOutput,
}: {
  action: PhaseAction
  onClose: () => void
  onRestoreFocus?: () => void
  loadOutput?: (ref: string) => Promise<string>
}) {
  const [opened, setOpened] = useState<string | null>(null)
  const [reads, setReads] = useState<Record<string, { content?: string; error?: string; loading?: boolean }>>({})
  const status = statusMeta(action)
  const refs = outputRefs(action.refs)

  const read = (ref: string) => {
    if (!loadOutput || reads[ref]?.loading) return
    setReads(prev => ({ ...prev, [ref]: { loading: true } }))
    void loadOutput(ref).then(
      content => setReads(prev => ({ ...prev, [ref]: { content } })),
      error => setReads(prev => ({ ...prev, [ref]: { error: error instanceof Error ? error.message : String(error) } })),
    )
  }

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent
        className="w-[calc(100vw-2rem)] max-w-[660px] gap-0 border-border bg-card p-0 shadow-xl"
        onCloseAutoFocus={(event) => {
          if (onRestoreFocus) {
            event.preventDefault()
            onRestoreFocus()
          }
        }}
      >
        <DialogHeader className="flex-row items-center gap-2.5 space-y-0 border-b border-border py-[15px] pl-[18px] pr-12">
          <DialogTitle className={cn("min-w-0 truncate rounded-md px-2.5 py-1 font-mono text-[12px] font-semibold", action.toolName === "advisor" ? "bg-advisor-soft text-advisor" : "bg-primary text-primary-foreground")}>
            {action.toolName}
          </DialogTitle>
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 text-[12px] font-semibold",
              status.tone === "success" && "text-status-success",
              status.tone === "running" && "text-status-running",
              status.tone === "failed" && "text-status-failed",
              status.tone === "neutral" && "text-muted-foreground",
            )}
          >
            <span
              className={cn(
                "h-[7px] w-[7px] rounded-full",
                status.tone === "success" && "bg-status-success",
                status.tone === "running" && "bg-status-running",
                status.tone === "failed" && "bg-status-failed",
                status.tone === "neutral" && "bg-muted-foreground",
              )}
            />
            {status.label}
          </span>
        </DialogHeader>
        <DialogDescription className="sr-only">
          Recorded tool output and the agent's observation.
        </DialogDescription>

        <div className="max-h-[72vh] space-y-3.5 overflow-auto px-[18px] py-4">
          {action.parameters && Object.keys(action.parameters).length ? (
            <details className="rounded-lg border border-border bg-muted px-3 py-2">
              <summary className="cursor-pointer text-[12px] font-semibold">Parameters</summary>
              <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px]">{JSON.stringify(action.parameters, null, 2)}</pre>
            </details>
          ) : null}
          <div className="space-y-1.5">
            <SectionLabel hint="raw tool result" title="Tool output" />
            {action.output?.trim() ? (
              <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted px-3 py-3 font-mono text-[12.5px] leading-relaxed text-foreground">
                {action.output}
              </pre>
            ) : (
              <div className="rounded-lg border border-dashed border-border bg-muted px-3 py-3 font-mono text-[12px] text-muted-foreground">
                no raw output captured
              </div>
            )}
            {refs.map((ref) => {
              const showFull = opened === ref.ref
              const content = reads[ref.ref]?.content ?? ref.content
              const error = reads[ref.ref]?.error
              return <div key={ref.ref}>
                <button
                  aria-expanded={showFull}
                  className="inline-flex items-center gap-2 py-0.5 font-mono text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  onClick={() => {
                    setOpened(showFull ? null : ref.ref)
                    if (!showFull && content == null) read(ref.ref)
                  }}
                  type="button"
                >
                  {showFull ? "hide full output" : "open full output"}
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-status-running-soft px-2.5 py-1 font-mono text-[11px] font-semibold text-status-running">
                    <span className="break-all">⌕ {ref.label || ref.ref}</span>
                  </span>
                </button>
                {showFull && content != null ? (
                  <pre className="mt-1.5 max-h-[40vh] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted px-3 py-3 font-mono text-[12.5px] leading-relaxed text-foreground">
                    {content || "(empty output)"}
                  </pre>
                ) : showFull ? (
                  <div role="status" className="mt-1.5 text-[12px] text-muted-foreground">
                    {error ? `Full output could not be read: ${error}` : reads[ref.ref]?.loading ? "Reading full output…" : "Full output was not included in this record."}
                    {error && loadOutput ? <button type="button" className="ml-2 underline" onClick={() => read(ref.ref)}>Retry</button> : null}
                  </div>
                ) : null}
              </div>
            })}
          </div>

          {action.observation ? (
            <div className="space-y-1.5">
              <SectionLabel hint="agent's interpretation" title="Observation" />
              <div className="rounded-lg border border-border bg-muted px-3 py-3 text-[13px] leading-relaxed text-muted-foreground">
                {action.observation}
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
