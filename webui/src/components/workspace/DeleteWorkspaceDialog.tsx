import { useState } from "react"
import { Loader2, Trash2 } from "lucide-react"

import { Button } from "@/components/common/Button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

export type DeleteWorkspaceKind = "workspace" | "launch"

export interface DeleteWorkspaceTarget {
  workspaceId: string
  label: string
  kind: DeleteWorkspaceKind
}

interface DeleteWorkspaceDialogProps {
  target: DeleteWorkspaceTarget
  onCancel: () => void
  onConfirm: (workspaceId: string) => Promise<void>
}

export function DeleteWorkspaceDialog({
  target,
  onCancel,
  onConfirm,
}: DeleteWorkspaceDialogProps) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isWorkspace = target.kind === "workspace"
  const title = isWorkspace ? "Delete workspace" : "Remove launch"
  const confirmLabel = isWorkspace ? "Delete workspace" : "Remove launch"

  const handleConfirm = async () => {
    setSubmitting(true)
    setError(null)

    try {
      await onConfirm(target.workspaceId)
    } catch (err) {
      setError(String(err))
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !submitting) {
          onCancel()
        }
      }}
    >
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[480px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle className="flex items-center gap-2 text-[13px] font-semibold text-slate-800">
            <Trash2 size={16} className="text-red-600" />
            {title}
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-slate-500">
            DELETE /api/workspaces/{target.workspaceId}
          </DialogDescription>
        </DialogHeader>

        <div className="px-4 py-4 text-[13px] leading-relaxed text-slate-600">
          <p>
            This removes <span className="font-medium text-slate-800">{target.label}</span>{" "}
            and cannot be undone. SAG will:
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-[12.5px] text-slate-600">
            <li>
              {isWorkspace
                ? "Stop and remove its Docker container"
                : "Remove its Docker container, if one exists"}
            </li>
            <li>Clear its queued and failed launch history</li>
          </ul>
        </div>

        {error ? (
          <div className="px-4 pb-1 text-[12px] text-red-600">{error}</div>
        ) : null}

        <DialogFooter className="gap-2 border-t border-slate-100 px-4 py-3 sm:space-x-0">
          <Button
            disabled={submitting}
            onClick={onCancel}
            type="button"
            variant="outline"
          >
            Cancel
          </Button>
          <Button
            disabled={submitting}
            onClick={() => void handleConfirm()}
            type="button"
            variant="destructive"
          >
            {submitting ? (
              <Loader2 className="animate-spin" size={13} />
            ) : (
              <Trash2 size={13} />
            )}
            {submitting ? "Removing" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
