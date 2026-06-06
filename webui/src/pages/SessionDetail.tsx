import { ArrowLeft, FileText, GitBranch, Plus, ShieldAlert } from "lucide-react"
import { useEffect, useState } from "react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card, CardHead } from "@/components/common/Card"
import { Tabs } from "@/components/common/Tabs"
import { BuildCard } from "@/components/session/BuildCard"
import { ContextMap } from "@/components/session/ContextMap"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestCard } from "@/components/session/TestCard"

const sessionTabs = ["Status", "Evidence", "Context", "Files", "Report", "Logs"] as const
type SessionTab = (typeof sessionTabs)[number]

function normalizeTab(tab?: string): SessionTab {
  const match = sessionTabs.find((candidate) => candidate.toLowerCase() === tab?.toLowerCase())
  return match ?? "Status"
}

interface Props {
  detail: ExecutionSessionDetail
  onBack: () => void
  onNewTask: (sourceSession: string) => void
  initialTab?: string
}

export function SessionDetail({ detail, onBack, onNewTask, initialTab }: Props) {
  const [tab, setTab] = useState<SessionTab>(() => normalizeTab(initialTab))

  useEffect(() => {
    setTab(normalizeTab(initialTab))
  }, [detail.id, initialTab])

  return (
    <main className="mx-auto max-w-[1000px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Button onClick={onBack} size="sm" type="button" variant="ghost">
              <ArrowLeft size={14} />
              Back
            </Button>
            <Badge mono>{detail.id}</Badge>
            <StatusBadge status={detail.status} />
            {detail.partial ? <Badge tone="amber">partial discovery</Badge> : null}
          </div>
          <h1 className="text-[22px] font-semibold tracking-tight text-slate-900">
            {detail.title}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[11px] text-slate-400">
            <span>{detail.workspace}</span>
            <span>/</span>
            <span>{detail.entry}</span>
            <span>/</span>
            <span>{detail.start}</span>
            <span>/</span>
            <span>{detail.duration}</span>
          </div>
        </div>
        <Button onClick={() => onNewTask(detail.id)} type="button">
          <Plus size={14} />
          New task from this
        </Button>
      </div>

      <Tabs
        className="mt-5"
        tabs={["Status", "Evidence", "Context", "Files", "Report", "Logs"]}
        value={tab}
        onChange={(value) => setTab(normalizeTab(value))}
      />

      <div className="mt-5">
        {tab === "Status" ? <StatusTab detail={detail} /> : null}
        {tab === "Evidence" ? <EvidenceTab detail={detail} /> : null}
        {tab === "Context" ? <ContextTab detail={detail} /> : null}
        {tab === "Files" ? <FilesTab detail={detail} /> : null}
        {tab === "Report" ? <ReportDoc doc={detail.reportDoc} /> : null}
        {tab === "Logs" ? <LogsView logs={detail.logs} /> : null}
      </div>
    </main>
  )
}

function StatusTab({ detail }: { detail: ExecutionSessionDetail }) {
  return (
    <div className="space-y-4">
      {detail.partial ? (
        <Card className="flex items-start gap-2.5 border-amber-200 bg-amber-50/50 p-3.5">
          <ShieldAlert size={15} className="mt-0.5 shrink-0 text-amber-500" />
          <div>
            <div className="text-[13px] font-semibold text-amber-700">
              Partially discovered session
            </div>
            <p className="mt-0.5 text-[12.5px] text-slate-600">
              Some runtime artifacts were recovered, but evidence, context, or file digests may be
              incomplete.
            </p>
          </div>
        </Card>
      ) : null}

      <Card className="overflow-hidden">
        <CardHead
          icon={<GitBranch size={16} className="text-slate-400" />}
          right={<StatusBadge status={detail.report} label={`Report: ${detail.report}`} />}
          sub={`${detail.entry} / ${detail.duration}`}
          title="Outcome"
        />
        <div className="px-4 py-3.5">
          <p className="text-[13px] leading-relaxed text-slate-600">{detail.outcome}</p>
        </div>
      </Card>

      {detail.blocker ? (
        <Card className="border-red-200 bg-red-50/40 p-4">
          <div className="flex items-center gap-2">
            <ShieldAlert size={15} className="text-red-500" />
            <span className="text-[13px] font-semibold text-red-700">{detail.blocker.title}</span>
            <span className="ml-auto font-mono text-[10px] text-red-400">{detail.blocker.code}</span>
          </div>
          <p className="mt-2 text-[12.5px] leading-relaxed text-slate-600">{detail.blocker.detail}</p>
          <p className="mt-2 font-mono text-[11px] text-red-600">{detail.blocker.hint}</p>
        </Card>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        <BuildCard build={detail.build} />
        <TestCard test={detail.test} />
      </div>

      {detail.report === "ready" && detail.reportDoc ? (
        <Card className="overflow-hidden">
          <CardHead
            icon={<FileText size={15} className="text-slate-400" />}
            sub={detail.reportDoc.title}
            title="Latest report"
          />
          <div className="px-4 py-3 text-[13px] leading-relaxed text-slate-500">
            {firstParagraph(detail.reportDoc.blocks) ?? "Report is ready."}
          </div>
        </Card>
      ) : null}
    </div>
  )
}

function EvidenceTab({ detail }: { detail: ExecutionSessionDetail }) {
  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<FileText size={16} className="text-slate-400" />}
        sub="Grouped by trusted runtime source"
        title="Evidence"
      />
      <EvidenceTimeline groups={detail.evidence} />
    </Card>
  )
}

function ContextTab({ detail }: { detail: ExecutionSessionDetail }) {
  if (!detail.context) {
    return (
      <Card className="px-4 py-10 text-center text-[13px] text-slate-400">
        Context map unavailable for this session.
      </Card>
    )
  }

  return <ContextMap ctx={detail.context} />
}

function FilesTab({ detail }: { detail: ExecutionSessionDetail }) {
  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<FileText size={16} className="text-slate-400" />}
        sub={detail.files ? `${detail.files.items.length} changed paths` : "unavailable"}
        title="Files"
      />
      <FilesDigest digest={detail.files} />
    </Card>
  )
}

function firstParagraph(blocks: Array<Record<string, unknown>>): string | null {
  const block = blocks.find((candidate) => candidate.type === "p" && typeof candidate.text === "string")

  return typeof block?.text === "string" ? block.text : null
}
