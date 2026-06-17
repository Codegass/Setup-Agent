import { Box, Plus, Send } from "lucide-react"
import { FormEvent, useState } from "react"

import type { WorkspaceSummary } from "@/api/types"
import { Button } from "@/components/common/Button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

export function NewTaskModal({
  workspace,
  sourceSession,
  onClose,
  onSubmit,
}: {
  workspace: WorkspaceSummary
  sourceSession?: string
  onClose: () => void
  onSubmit: (task: string, sourceSession?: string) => Promise<void>
}) {
  const [task, setTask] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = task.trim()

    if (!trimmed) {
      setError("Task description is required.")
      return
    }

    setSubmitting(true)
    setError(null)

    try {
      await onSubmit(trimmed, sourceSession)
    } catch (err) {
      setError(String(err))
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose()
        }
      }}
    >
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[520px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle className="flex items-center gap-2 text-[13px] font-semibold text-slate-800">
            <Plus size={16} className="text-blue-600" />
            New task
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-slate-500">
            Creates a new execution session in {workspace.id}
          </DialogDescription>
        </DialogHeader>
        <form className="p-4" onSubmit={handleSubmit}>
          {sourceSession ? (
            <div className="mb-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-[12px] text-blue-700">
              Prefilled from <span className="font-mono">{sourceSession}</span>. This starts a new
              workspace task, not a continuation of the session as chat.
            </div>
          ) : null}
          <label
            className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500"
            htmlFor="workspace-task"
          >
            Task description
          </label>
          <textarea
            autoFocus
            className="mt-1.5 w-full resize-none rounded-md border border-slate-200 p-3 text-[13px] text-slate-700 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
            id="workspace-task"
            onChange={(event) => setTask(event.target.value)}
            placeholder="e.g. add a health check and run the smoke tests"
            rows={4}
            value={task}
          />
          <div className="mt-2 flex items-center gap-2 font-mono text-[10.5px] text-slate-500">
            <Box size={12} />
            POST /api/workspaces/{workspace.id}/tasks
          </div>
          {error ? <div className="mt-3 text-[12px] text-red-600">{error}</div> : null}
          <DialogFooter className="mt-4 gap-2 sm:space-x-0">
            <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={submitting} type="submit">
              <Send size={13} />
              {submitting ? "Submitting" : "Submit task"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
