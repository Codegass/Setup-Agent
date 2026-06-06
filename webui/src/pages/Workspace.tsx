import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Box,
  FileText,
  GitBranch,
  Plus,
  Send,
  Settings as SettingsIcon,
  Terminal,
} from "lucide-react"
import { FormEvent, useEffect, useMemo, useState } from "react"

import type {
  BuildSummary,
  ExecutionSessionDetail,
  SubmitTaskResponse,
  TestSummary,
  WorkspaceSummary,
} from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card, CardHead } from "@/components/common/Card"
import { Tabs } from "@/components/common/Tabs"
import { TestBar } from "@/components/common/TestBar"
import { BuildCard } from "@/components/session/BuildCard"
import { ContextMap } from "@/components/session/ContextMap"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { TestCard } from "@/components/session/TestCard"
import { cn } from "@/lib/utils"

const workspaceTabs = ["Overview", "Sessions", "Terminal", "Settings"] as const
type WorkspaceTab = (typeof workspaceTabs)[number]

export interface WorkspaceSessionRow {
  id: string
  title: string
  status: string
  entry: string
  start: string
  duration: string
  build: BuildSummary
  test: TestSummary
  evidenceCount: number | null
  filesCount: number | null
}

interface WorkspaceProps {
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
  sessions: WorkspaceSessionRow[]
  onBack: () => void
  onOpenSession: (sessionId: string, tab?: string) => void
  onSubmitTask: (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ) => Promise<SubmitTaskResponse>
  initialTaskSourceSession?: string | null
}

export function Workspace({
  workspace,
  latest,
  sessions,
  onBack,
  onOpenSession,
  onSubmitTask,
  initialTaskSourceSession,
}: WorkspaceProps) {
  const [tab, setTab] = useState<WorkspaceTab>("Overview")
  const [modal, setModal] = useState<{ sourceSession?: string } | null>(null)
  const [submitted, setSubmitted] = useState<SubmitTaskResponse | null>(null)

  useEffect(() => {
    setTab("Overview")
    setSubmitted(null)
    setModal(initialTaskSourceSession ? { sourceSession: initialTaskSourceSession } : null)
  }, [workspace.id, initialTaskSourceSession])

  const fallbackBuild = useMemo(() => normalizeWorkspaceBuild(workspace.build), [workspace.build])
  const displayBuild = latest?.build ?? fallbackBuild
  const displayTest = latest?.test ?? workspace.test

  return (
    <main className="mx-auto max-w-[1000px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Button onClick={onBack} size="sm" type="button" variant="ghost">
              <ArrowLeft size={14} />
              Back
            </Button>
            <Badge mono>{workspace.id}</Badge>
            <StatusBadge status={workspace.docker.status} />
            {workspace.release ? <Badge mono>{workspace.release}</Badge> : null}
          </div>
          <h1 className="text-[22px] font-semibold tracking-tight text-slate-900">
            {workspace.project}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[11px] text-slate-400">
            <span>{workspace.container}</span>
            <span>/</span>
            <span>{workspace.stack}</span>
            {workspace.commit ? (
              <>
                <span>/</span>
                <span>{workspace.commit}</span>
              </>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => setTab("Terminal")} type="button" variant="outline">
            <Terminal size={14} />
            Shell
          </Button>
          <Button onClick={() => setModal({})} type="button">
            <Plus size={14} />
            New task
          </Button>
        </div>
      </div>

      <Tabs
        className="mt-5"
        tabs={[
          "Overview",
          { id: "Sessions", label: "Sessions", count: sessions.length },
          "Terminal",
          "Settings",
        ]}
        value={tab}
        onChange={(value) => setTab(normalizeWorkspaceTab(value))}
      />

      <div className="mt-5">
        {submitted ? (
          <Card className="mb-4 border-blue-100 bg-blue-50/50 px-4 py-3 text-[13px] text-blue-700">
            Task queued as <span className="font-mono">{submitted.session_id}</span>
            {submitted.source_session ? (
              <>
                {" "}
                from <span className="font-mono">{submitted.source_session}</span>
              </>
            ) : null}
            .
          </Card>
        ) : null}

        {tab === "Overview" ? (
          <OverviewTab
            build={displayBuild}
            latest={latest}
            onOpenSession={onOpenSession}
            test={displayTest}
            workspace={workspace}
          />
        ) : null}
        {tab === "Sessions" ? (
          <SessionsTab onOpenSession={onOpenSession} rows={sessions} />
        ) : null}
        {tab === "Terminal" ? <TerminalTab workspace={workspace} /> : null}
        {tab === "Settings" ? <SettingsTab latest={latest} workspace={workspace} /> : null}
      </div>

      {modal ? (
        <NewTaskModal
          onClose={() => setModal(null)}
          onSubmit={async (task, sourceSession) => {
            const response = await onSubmitTask(workspace.id, task, sourceSession)
            setSubmitted(response)
            setModal(null)
          }}
          sourceSession={modal.sourceSession}
          workspace={workspace}
        />
      ) : null}
    </main>
  )
}

function OverviewTab({
  workspace,
  latest,
  build,
  test,
  onOpenSession,
}: {
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
  build: BuildSummary
  test: TestSummary
  onOpenSession: (sessionId: string, tab?: string) => void
}) {
  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">
                {workspace.activeSession ? "Active session" : "Latest session"}
              </span>
              {latest ? <span className="font-mono text-[11px] text-slate-400">{latest.id}</span> : null}
              {latest ? <StatusBadge status={latest.status} /> : null}
            </div>
            <div className="mt-1 truncate text-[14px] font-medium text-slate-800">
              {latest?.title ?? workspace.task}
            </div>
          </div>
          {latest ? (
            <Button
              onClick={() => onOpenSession(latest.id)}
              size="sm"
              type="button"
              variant="ghost"
            >
              Open session detail
              <ArrowRight size={13} />
            </Button>
          ) : workspace.latestSession ? (
            <Button
              onClick={() => onOpenSession(workspace.latestSession as string)}
              size="sm"
              type="button"
              variant="ghost"
            >
              Open session detail
              <ArrowRight size={13} />
            </Button>
          ) : null}
        </div>
        <div className="px-4 py-3.5">
          <div className="text-[13px] leading-relaxed text-slate-600">
            {latest?.outcome ?? workspace.task}
          </div>
          {!latest && workspace.latestSession ? (
            <div className="mt-2 font-mono text-[11px] text-slate-400">
              Fetching latest session detail from /api/sessions/{workspace.latestSession}.
            </div>
          ) : null}
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <BuildCard build={build} />
        <TestCard test={test} />
      </div>

      {latest?.report === "ready" && latest.reportDoc ? (
        <Card className="overflow-hidden">
          <CardHead
            icon={<FileText size={15} className="text-slate-400" />}
            right={
              <Button
                onClick={() => onOpenSession(latest.id, "Report")}
                size="sm"
                type="button"
                variant="ghost"
              >
                View
                <ArrowRight size={13} />
              </Button>
            }
            sub={latest.reportDoc.title}
            title="Latest report"
          />
          <div className="px-4 py-3 text-[13px] leading-relaxed text-slate-500">
            {firstParagraph(latest.reportDoc.blocks) ?? "Report is ready."}
          </div>
        </Card>
      ) : null}

      {latest && !latest.partial ? (
        <div className="grid gap-4 md:grid-cols-2">
          <Card className="overflow-hidden">
            <CardHead
              icon={<FileText size={15} className="text-slate-400" />}
              right={
                <Button
                  onClick={() => onOpenSession(latest.id, "Files")}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <ArrowRight size={13} />
                </Button>
              }
              sub={latest.files ? `${latest.files.items.length} since startup` : "unavailable"}
              title="File changes"
            />
            <FilesDigest digest={latest.files} preview />
          </Card>
          <Card className="overflow-hidden">
            <CardHead
              icon={<Activity size={15} className="text-slate-400" />}
              right={
                <Button
                  onClick={() => onOpenSession(latest.id, "Evidence")}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <ArrowRight size={13} />
                </Button>
              }
              sub="by trusted source"
              title="Evidence"
            />
            <EvidenceTimeline groups={latest.evidence} preview />
          </Card>
        </div>
      ) : null}

      {latest?.context && !latest.partial ? (
        <div>
          <div className="mb-2 flex items-center justify-between">
            <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">
              Context map - trunk / branch
            </div>
            <Button
              onClick={() => onOpenSession(latest.id, "Context")}
              size="sm"
              type="button"
              variant="ghost"
            >
              Full context
              <ArrowRight size={13} />
            </Button>
          </div>
          <ContextMap ctx={latest.context} preview />
        </div>
      ) : null}
    </div>
  )
}

function SessionsTab({
  rows,
  onOpenSession,
}: {
  rows: WorkspaceSessionRow[]
  onOpenSession: (sessionId: string, tab?: string) => void
}) {
  if (!rows.length) {
    return (
      <Card className="p-10 text-center text-[13px] text-slate-400">
        No sessions are available from the current dashboard and latest-session API data.
      </Card>
    )
  }

  return (
    <Card className="overflow-hidden">
      <div className="hidden grid-cols-[1.8fr_0.8fr_0.8fr_1fr_0.5fr_0.5fr_40px] items-center gap-3 border-b border-slate-100 bg-slate-50/60 px-4 py-2.5 lg:grid">
        {["Task", "Status", "Entry", "Build / test", "Evid.", "Files", ""].map((header) => (
          <div key={header} className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
            {header}
          </div>
        ))}
      </div>
      <div className="divide-y divide-slate-100">
        {rows.map((session) => (
          <button
            key={session.id}
            className="group grid w-full cursor-pointer gap-3 px-4 py-3 text-left hover:bg-slate-50/70 lg:grid-cols-[1.8fr_0.8fr_0.8fr_1fr_0.5fr_0.5fr_40px] lg:items-center"
            onClick={() => onOpenSession(session.id)}
            type="button"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] text-slate-300">{session.id}</span>
                <span className="truncate text-[13px] font-medium text-slate-700 group-hover:text-blue-600">
                  {session.title}
                </span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-slate-400">
                {session.start} / {session.duration}
              </div>
            </div>
            <div>
              <StatusBadge status={session.status} />
            </div>
            <div>
              <Badge mono>{session.entry}</Badge>
            </div>
            <div className="flex items-center gap-2">
              <StatusBadge dot={false} status={session.build.state} />
              {session.test.total > 0 ? (
                <TestBar fail={session.test.fail} pass={session.test.pass} total={session.test.total} />
              ) : (
                <span className="text-[11px] text-slate-300">no tests</span>
              )}
            </div>
            <div className="font-mono text-[12px] text-slate-500">
              {session.evidenceCount ?? "-"}
            </div>
            <div className="font-mono text-[12px] text-slate-500">{session.filesCount ?? "-"}</div>
            <div className="flex justify-end opacity-0 transition-opacity group-hover:opacity-100">
              <ArrowRight size={15} className="text-slate-400" />
            </div>
          </button>
        ))}
      </div>
    </Card>
  )
}

function TerminalTab({ workspace }: { workspace: WorkspaceSummary }) {
  const running = workspace.docker.status.trim().toLowerCase() === "running"

  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<Terminal size={16} className="text-slate-400" />}
        right={<StatusBadge status={running ? "pending" : workspace.docker.status} />}
        sub="Static placeholder until Task 16"
        title="Independent workspace shell"
      />
      <div className="p-5">
        <div className="rounded-lg border border-slate-800 bg-[#0d1117]">
          <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-2">
            <div className="flex gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-red-400/80" />
              <span className="h-2.5 w-2.5 rounded-full bg-amber-400/80" />
              <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/80" />
            </div>
            <span className="ml-2 font-mono text-[11px] text-slate-400">
              {workspace.container} - shell inactive
            </span>
          </div>
          <div className="px-4 py-6 font-mono text-[12.5px] leading-relaxed text-slate-300">
            <div>$ sag workspace shell</div>
            <div className="text-slate-500">Terminal is not connected in Task 15.</div>
            <div className="text-slate-500">
              This workspace shell is isolated from session state and does not continue any session
              as chat.
            </div>
          </div>
        </div>
        <p className="mt-3 text-[12px] leading-relaxed text-slate-400">
          Terminal commands will be an independent workspace operation when Task 16 wires the shell.
          Session details above remain read-only execution records.
        </p>
      </div>
    </Card>
  )
}

function SettingsTab({
  workspace,
  latest,
}: {
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <SettingsCard icon={<Box size={15} className="text-slate-400" />} title="Container">
        <SettingsRow label="Name" value={workspace.container} />
        <SettingsRow label="Status" value={<StatusBadge status={workspace.docker.status} />} />
        <SettingsRow label="Image" value={workspace.docker.image ?? "unknown"} />
        <SettingsRow label="Endpoint" value={workspace.docker.endpoint ?? "local Docker"} />
      </SettingsCard>
      <SettingsCard icon={<GitBranch size={15} className="text-slate-400" />} title="Workspace">
        <SettingsRow label="Project" value={workspace.project} />
        <SettingsRow label="Stack" value={workspace.stack} />
        <SettingsRow label="Tag" value={workspace.tag ?? "untracked"} />
        <SettingsRow label="Commit" value={workspace.commit ?? "unknown"} />
      </SettingsCard>
      <SettingsCard icon={<Activity size={15} className="text-slate-400" />} title="Sessions">
        <SettingsRow label="Active" value={workspace.activeSession ?? "none"} />
        <SettingsRow label="Latest" value={workspace.latestSession ?? "none"} />
        <SettingsRow label="Latest status" value={latest ? <StatusBadge status={latest.status} /> : "not loaded"} />
        <SettingsRow label="Updated" value={workspace.updated} />
      </SettingsCard>
      <SettingsCard icon={<SettingsIcon size={15} className="text-slate-400" />} title="Read model">
        <SettingsRow label="Build" value={normalizeWorkspaceBuild(workspace.build).state} />
        <SettingsRow label="Test" value={workspace.test.state} />
        <SettingsRow label="Report" value={workspace.report} />
        <SettingsRow label="Changed files" value={String(workspace.changed)} />
      </SettingsCard>
    </div>
  )
}

function SettingsCard({
  title,
  icon,
  children,
}: {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card className="p-4">
      <div className="mb-1.5 flex items-center gap-2">
        {icon}
        <span className="text-[13px] font-semibold text-slate-800">{title}</span>
      </div>
      {children}
    </Card>
  )
}

function SettingsRow({
  label,
  value,
}: {
  label: string
  value: React.ReactNode
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-slate-100 py-2 last:border-b-0">
      <span className="text-[12.5px] text-slate-500">{label}</span>
      <span
        className={cn(
          "min-w-0 truncate text-right text-[12.5px] text-slate-700",
          typeof value === "string" && "font-mono text-[12px]",
        )}
      >
        {value}
      </span>
    </div>
  )
}

function NewTaskModal({
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
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/30 p-4"
      onClick={onClose}
    >
      <Card
        className="w-full max-w-[520px] shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <CardHead
          icon={<Plus size={16} className="text-blue-600" />}
          sub={`Creates a new execution session in ${workspace.id}`}
          title="New task"
        />
        <form className="p-4" onSubmit={handleSubmit}>
          {sourceSession ? (
            <div className="mb-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-[12px] text-blue-700">
              Prefilled from <span className="font-mono">{sourceSession}</span>. This starts a new
              workspace task, not a continuation of the session as chat.
            </div>
          ) : null}
          <label
            className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400"
            htmlFor="workspace-task"
          >
            Task description
          </label>
          <textarea
            className="mt-1.5 w-full resize-none rounded-md border border-slate-200 p-3 text-[13px] text-slate-700 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
            id="workspace-task"
            onChange={(event) => setTask(event.target.value)}
            placeholder="e.g. add a health check and run the smoke tests"
            rows={4}
            value={task}
          />
          <div className="mt-2 flex items-center gap-2 font-mono text-[10.5px] text-slate-400">
            <Box size={12} />
            POST /api/workspaces/{workspace.id}/tasks
          </div>
          {error ? <div className="mt-3 text-[12px] text-red-600">{error}</div> : null}
          <div className="mt-4 flex justify-end gap-2">
            <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={submitting} type="submit">
              <Send size={13} />
              {submitting ? "Submitting" : "Submit task"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}

function normalizeWorkspaceTab(tab: string): WorkspaceTab {
  const match = workspaceTabs.find((candidate) => candidate === tab)
  return match ?? "Overview"
}

function normalizeWorkspaceBuild(build: WorkspaceSummary["build"]): BuildSummary {
  if (typeof build === "string") {
    return { state: build, tool: "", time: "", note: "" }
  }

  return build
}

function firstParagraph(blocks: Array<Record<string, unknown>>): string | null {
  const block = blocks.find((candidate) => candidate.type === "p" && typeof candidate.text === "string")

  return typeof block?.text === "string" ? block.text : null
}
