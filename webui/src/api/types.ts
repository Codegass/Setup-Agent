export type Tone = "neutral" | "blue" | "green" | "red" | "amber"

export interface DockerSummary {
  status: string
  image?: string | null
  version?: string | null
  endpoint?: string | null
}

export interface TestSummary {
  state: string
  pass: number
  fail: number
  skip: number
  total: number
  note?: string
}

export interface BuildSummary {
  state: string
  tool: string
  time: string
  artifact?: string | null
  note: string
}

export interface WorkspaceSummary {
  id: string
  project: string
  container: string
  stack: string
  tag?: string | null
  release?: string | null
  commit?: string | null
  docker: DockerSummary
  task: string
  build: BuildSummary | string
  test: TestSummary
  report: string
  changed: number
  activeSession?: string | null
  latestSession?: string | null
  sessions?: ExecutionSessionSummary[]
  updated: string
}

export interface ExecutionSessionSummary {
  id: string
  workspace: string
  title: string
  status: string
  entry: string
  start: string
  finish?: string | null
  duration: string
  build: string
  test: TestSummary
  report: string
  files: number
  evidence: number
}

export interface DashboardResponse {
  docker: DockerSummary
  workspaces: WorkspaceSummary[]
}

export interface EvidenceRecord {
  time: string
  status: string
  title: string
  detail: string
  ref: string
}

export interface EvidenceGroup {
  source: string
  status: string
  counts: string
  time: string
  summary: string
  records: EvidenceRecord[]
}

export interface ExecutionSessionDetail {
  id: string
  workspace: string
  title: string
  status: string
  entry: string
  start: string
  duration: string
  outcome: string
  build: BuildSummary
  test: TestSummary
  report: string
  reportDoc?: ReportDocument | null
  blocker?: { code: string; title: string; detail: string; hint: string } | null
  evidence: EvidenceGroup[]
  files?: FileChangeDigest | null
  context?: ContextMap | null
  logs: string[]
  partial?: boolean
}

export interface FileChangeDigest {
  snapshot: { base: string; head: string; mode: string }
  counts: { modified: number; added: number; deleted: number; renamed: number }
  items: Array<{
    path: string
    change: string
    type: string
    size: string
    mtime: string
    note: string
  }>
}

export interface ContextMap {
  trunk: {
    goal: string
    state: string
    progress: Record<string, number>
    summary: string
  }
  tasks: Array<{
    id: string
    title: string
    status: string
    summary: string
    refs: string[]
    recovered: boolean
  }>
  activeBranch: {
    task: string
    why: string
    memory: string[]
    lastRefs: Array<Record<string, string>>
    pressure: number
  }
  debug: Record<string, unknown>
}

export interface SubmitTaskResponse {
  workspace_id: string
  session_id: string
  source_session: string | null
  status: string
}

export interface ReportDocument {
  title: string
  path?: string | null
  generated: string
  blocks: Array<Record<string, unknown>>
}
