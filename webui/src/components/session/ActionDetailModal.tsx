import { useState } from "react"

import type { ContextReference, ContextTrace } from "@/api/types"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

type PhaseAction =
  ContextTrace["phases"][number]["tasks"][number]["iterations"][number]["actions"][number]
type ContextRef = ContextReference | string

/** The honest status the modal reports — pending dispatch shows running, not a
 *  premature success/failure. */
function statusMeta(action: PhaseAction): { label: string; tone: "success" | "running" | "failed" } {
  if (action.dispatchStatus === "pending") return { label: "running", tone: "running" }
  if (action.success === true) return { label: "success", tone: "success" }
  return { label: "failed", tone: "failed" }
}

/** First ref that carries fuller content than the (possibly truncated) inline
 *  output, so the modal can offer "open full output". */
function fullRef(refs: ContextRef[]): ContextReference | null {
  for (const ref of refs ?? []) {
    if (typeof ref !== "string" && ref.content) return ref
  }
  return null
}

function SectionLabel({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">
        {title}
      </span>
      <span className="text-[11px] text-slate-300">{hint}</span>
    </div>
  )
}

export function ActionDetailModal({
  action,
  onClose,
}: {
  action: PhaseAction
  onClose: () => void
}) {
  const [showFull, setShowFull] = useState(false)
  const status = statusMeta(action)
  const ref = fullRef(action.refs)

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[660px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="flex-row items-center gap-2.5 border-b border-slate-100 px-[18px] py-[15px]">
          <DialogTitle className="flex h-[22px] items-center rounded-md bg-slate-700 px-2.5 font-mono text-[12px] font-semibold text-slate-200">
            {action.toolName}
          </DialogTitle>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 text-[12px] font-semibold",
              status.tone === "success" && "text-status-success",
              status.tone === "running" && "text-status-running",
              status.tone === "failed" && "text-status-failed",
            )}
          >
            <span
              className={cn(
                "h-[7px] w-[7px] rounded-full",
                status.tone === "success" && "bg-status-success",
                status.tone === "running" && "bg-status-running",
                status.tone === "failed" && "bg-status-failed",
              )}
            />
            {status.label}
          </span>
        </DialogHeader>

        <div className="max-h-[72vh] space-y-3.5 overflow-auto px-[18px] py-4">
          <div className="space-y-1.5">
            <SectionLabel hint="raw tool result" title="Tool output" />
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-lg border border-slate-100 bg-slate-50 px-3 py-3 font-mono text-[12.5px] leading-relaxed text-slate-700">
              {action.output}
            </pre>
            {ref ? (
              <div>
                <button
                  aria-expanded={showFull}
                  className="inline-flex items-center gap-2 py-0.5 font-mono text-[11px] text-slate-400 transition-colors hover:text-slate-600"
                  onClick={() => setShowFull((value) => !value)}
                  type="button"
                >
                  {showFull ? "hide full output" : "open full output"}
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-status-running-soft px-2.5 py-1 font-mono text-[11px] font-semibold text-status-running">
                    ⌕ {ref.label || ref.ref}
                  </span>
                </button>
                {showFull ? (
                  <pre className="mt-1.5 max-h-[40vh] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-100 bg-slate-50 px-3 py-3 font-mono text-[12.5px] leading-relaxed text-slate-700">
                    {ref.content}
                  </pre>
                ) : null}
              </div>
            ) : null}
          </div>

          {action.observation ? (
            <div className="space-y-1.5">
              <SectionLabel hint="agent's interpretation" title="Observation" />
              <div className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-3 text-[13px] leading-relaxed text-slate-600">
                {action.observation}
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
