import { DetailHeader } from "sag-workbench"
import { detail, workspaces } from "./_fixtures"

// workspaces[0] is the acme-platform WorkspaceSummary; enrich with a session list
// so the session switcher row (sessions.length > 1) renders too.
const workspace = {
  ...workspaces[0],
  release: "rel/acme-2.3.0",
  sessions: [
    {
      id: detail.id,
      workspace: detail.workspace,
      title: detail.title,
      status: detail.status,
      entry: detail.entry,
      start: detail.start,
      duration: detail.duration,
      build: "success",
      test: detail.test,
      evidenceStatus: detail.evidenceStatus,
      report: detail.report,
      files: 5,
      evidence: 2,
    },
    {
      id: "SETUP-acme-20260617-201145",
      workspace: detail.workspace,
      title: "acme-platform first pass",
      status: "failed",
      entry: detail.entry,
      start: "2026-06-17 20:11:45",
      duration: "6m 12s",
      build: "failure",
      test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 },
      evidenceStatus: "partial",
      report: "—",
      files: 0,
      evidence: 1,
    },
  ],
}

export const WithSessions = () => (
  <div style={{ width: 820 }}>
    <DetailHeader
      detail={detail}
      onDelete={() => {}}
      onNewTask={() => {}}
      onSession={() => {}}
      onSettings={() => {}}
      onTerminal={() => {}}
      sessionId={detail.id}
      workspace={workspace}
    />
  </div>
)

export const SingleSession = () => (
  <div style={{ width: 820 }}>
    <DetailHeader
      detail={detail}
      onDelete={() => {}}
      onNewTask={() => {}}
      onSession={() => {}}
      onSettings={() => {}}
      onTerminal={() => {}}
      sessionId={detail.id}
      workspace={workspaces[0]}
    />
  </div>
)
