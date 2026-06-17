import { useMemo, useState } from "react"

import type { ExecutionSessionDetail, SubmitTaskResponse, WorkspaceSummary } from "@/api/types"
import { NewTaskModal } from "@/components/workspace/NewTaskModal"
import {
  WorkspacePanel,
  type WorkspacePanelKind,
} from "@/components/workspace/WorkspacePanels"
import {
  DeleteWorkspaceDialog,
  type DeleteWorkspaceTarget,
} from "@/components/workspace/DeleteWorkspaceDialog"

import { DetailHeader } from "./DetailHeader"
import { SectionNav } from "./SectionNav"
import { SummaryBand } from "./SummaryBand"
import { buildDetailFacets, FacetBody, type FacetId } from "./facets"
import { useScrollSpy } from "./scrollSpy"

export function DetailPane({
  workspace,
  detail,
  sessionId,
  onSession,
  onSubmitTask,
  onDelete,
}: {
  workspace: WorkspaceSummary
  detail: ExecutionSessionDetail
  sessionId: string
  onSession: (sessionId: string) => void
  onSubmitTask: (workspaceId: string, task: string, sourceSession?: string) => Promise<SubmitTaskResponse>
  onDelete: (workspaceId: string) => Promise<void>
}) {
  const facets = useMemo(() => buildDetailFacets(detail), [detail])
  const ids = useMemo(() => facets.map((f) => f.id), [facets])
  const { containerRef, active, onScroll, jump } = useScrollSpy(ids)

  const [panel, setPanel] = useState<WorkspacePanelKind | null>(null)
  const [taskOpen, setTaskOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<DeleteWorkspaceTarget | null>(null)

  return (
    <div className="mx-auto flex h-[calc(100vh-3rem)] max-w-[1180px] flex-col">
      <DetailHeader
        onDelete={() =>
          setDeleteTarget({ workspaceId: workspace.id, label: workspace.project, kind: "workspace" })
        }
        onNewTask={() => setTaskOpen(true)}
        onSession={onSession}
        onSettings={() => setPanel("settings")}
        onTerminal={() => setPanel("terminal")}
        sessionId={sessionId}
        workspace={workspace}
      />

      <div className="flex min-h-0 flex-1">
        {/* Left section-nav (sticky within the flex row) */}
        <aside className="hidden w-44 shrink-0 overflow-y-auto border-r border-slate-200 px-3 py-6 lg:block">
          <SectionNav active={active} facets={facets} onJump={(id: FacetId) => jump(id)} />
        </aside>

        {/* Right continuous scroll */}
        <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-7" onScroll={onScroll}>
          <SummaryBand detail={detail} />
          <div className="mt-7 space-y-7">
            {facets.map((f) => (
              <section key={f.id} className="scroll-mt-[150px]" id={`facet-${f.id}`}>
                <div className="mb-2.5 flex items-center gap-2">
                  <f.icon className="text-slate-400" size={14} />
                  <h3 className="text-[13px] font-semibold tracking-tight text-slate-700">{f.label}</h3>
                  <div className="ml-1 h-px flex-1 bg-slate-100" />
                </div>
                <FacetBody detail={detail} id={f.id} />
              </section>
            ))}
          </div>
          <div className="h-16" />
        </div>
      </div>

      {panel ? (
        <WorkspacePanel kind={panel} latest={detail} onClose={() => setPanel(null)} workspace={workspace} />
      ) : null}

      {taskOpen ? (
        <NewTaskModal
          onClose={() => setTaskOpen(false)}
          onSubmit={async (task, sourceSession) => {
            await onSubmitTask(workspace.id, task, sourceSession)
            setTaskOpen(false)
          }}
          sourceSession={sessionId}
          workspace={workspace}
        />
      ) : null}

      {deleteTarget ? (
        <DeleteWorkspaceDialog
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (id) => {
            await onDelete(id)
            setDeleteTarget(null)
          }}
          target={deleteTarget}
        />
      ) : null}
    </div>
  )
}
