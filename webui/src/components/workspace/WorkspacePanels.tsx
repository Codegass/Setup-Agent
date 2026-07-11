import { Terminal as TerminalIcon, X } from "lucide-react"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { TerminalPanel } from "@/components/terminal/TerminalPanel"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

import { WorkspaceSettings } from "./WorkspaceSettings"

export type WorkspacePanelKind = "terminal" | "settings"

export function WorkspacePanel({
  kind,
  workspace,
  latest,
  onClose,
}: {
  kind: WorkspacePanelKind
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
  onClose: () => void
}) {
  const running = workspace.docker.status.trim().toLowerCase() === "running"
  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[760px] gap-0 border-border bg-card p-0 shadow-xl">
        <DialogHeader className="space-y-1 border-b border-border px-4 py-3">
          <div className="flex flex-row items-center justify-between">
            <DialogTitle className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
              {kind === "terminal" ? <TerminalIcon className="text-muted-foreground" size={16} /> : null}
              {kind === "terminal" ? "Terminal" : "Settings"}
            </DialogTitle>
            {kind === "terminal" ? <StatusBadge status={workspace.docker.status} /> : null}
          </div>
          <DialogDescription className="font-mono text-[11px] text-muted-foreground">
            {kind === "terminal"
              ? `Interactive shell for ${workspace.container}`
              : `Workspace settings for ${workspace.container}`}
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-auto p-4">
          {kind === "settings" ? (
            <WorkspaceSettings latest={latest} workspace={workspace} />
          ) : running ? (
            <TerminalPanel workspaceId={workspace.id} />
          ) : (
            <div className="rounded-lg border border-border bg-muted px-4 py-6">
              <div className="text-[13px] font-medium text-foreground">Container is not running</div>
              <div className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
                Start the workspace container before opening an interactive shell.
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
