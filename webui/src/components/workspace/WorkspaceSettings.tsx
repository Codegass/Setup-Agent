import { Activity, Box, GitBranch, Settings as SettingsIcon } from "lucide-react"
import type * as React from "react"

import type { BuildSummary, ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { cn } from "@/lib/utils"

export function WorkspaceSettings({
  workspace,
  latest,
}: {
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <SettingsCard icon={<Box size={15} className="text-slate-500" />} title="Container">
        <SettingsRow label="Name" value={workspace.container} />
        <SettingsRow label="Status" value={<StatusBadge status={workspace.docker.status} />} />
        <SettingsRow label="Image" value={workspace.docker.image ?? "unknown"} />
        <SettingsRow label="Endpoint" value={workspace.docker.endpoint ?? "local Docker"} />
      </SettingsCard>
      <SettingsCard icon={<GitBranch size={15} className="text-slate-500" />} title="Workspace">
        <SettingsRow label="Project" value={workspace.project} />
        <SettingsRow label="Stack" value={workspace.stack} />
        <SettingsRow label="Tag" value={workspace.tag ?? "untracked"} />
        <SettingsRow label="Commit" value={workspace.commit ?? "unknown"} />
      </SettingsCard>
      <SettingsCard icon={<Activity size={15} className="text-slate-500" />} title="Sessions">
        <SettingsRow label="Active" value={workspace.activeSession ?? "none"} />
        <SettingsRow label="Latest" value={workspace.latestSession ?? "none"} />
        <SettingsRow label="Latest status" value={latest ? <StatusBadge status={latest.status} /> : "not loaded"} />
        <SettingsRow label="Updated" value={workspace.updated} />
      </SettingsCard>
      <SettingsCard icon={<SettingsIcon size={15} className="text-slate-500" />} title="Read model">
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

function normalizeWorkspaceBuild(build: WorkspaceSummary["build"]): BuildSummary {
  if (typeof build === "string") {
    return { state: build, tool: "", time: "", note: "" }
  }

  return build
}
