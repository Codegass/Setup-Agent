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
  errors?: number
  passRate?: number | null
  executionRate?: number | null
  reportFileCount?: number | null
  uniqueTotal?: number | null
  uniquePassed?: number | null
  uniqueFailed?: number | null
  uniqueErrors?: number | null
  uniqueSkipped?: number | null
  declaredTotal?: number | null
  methodExecutionRate?: number | null
  failingNames?: string[]
  conflicts?: string[]
  evidenceRefs?: string[]
  note?: string
}

export interface BuildSummary {
  state: string
  tool: string
  time: string
  artifact?: string | null
  note: string
  system?: string | null
  classCount?: number | null
  jarCount?: number | null
  moduleOutputCount?: number | null
  artifactSamples?: string[]
  warnings?: string[]
  evidenceRefs?: string[]
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
  evidenceStatus?: string | null
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
  evidenceStatus?: string | null
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
  evidenceStatus?: string | null
  build: BuildSummary
  test: TestSummary
  report: string
  reportDoc?: ReportDocument | null
  blocker?: { code: string; title: string; detail: string; hint: string } | null
  evidence: EvidenceGroup[]
  files?: FileChangeDigest | null
  context?: ContextTrace | null
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

export interface ContextTrace {
  trunk: {
    goal: string
    state: string
    progress: Record<string, number>
    summary: string
  }
  phases: Array<{
    id: string
    name: string
    title: string
    status: string
    notes?: string
    keyResults?: string
    evidenceStatus?: string | null
    evidenceRefs?: Array<ContextReference | string> | null
    conflicts?: string[] | null
    refs: Array<ContextReference | string>
    progress: Record<string, number>
    tasks: Array<{
      id: string
      title: string
      status: string
      iterations: Array<{
        iteration?: number | null
        sequence: number
        thoughts: string[]
        actions: Array<{
          toolName: string
          success?: boolean | null
          parameters?: Record<string, unknown>
          output: string
          observation: string
          refs: Array<ContextReference | string>
          dispatchStatus?: string | null
        }>
        window?: {
          totalChars: number
          stepSpan?: number | null
          segments: Record<string, unknown>
          delta: Record<string, unknown>
          introText?: string | null
          ledgerText?: string | null
        } | null
      }>
    }>
  }>
  debug: Record<string, unknown>
}

export interface ContextReference {
  ref: string
  label: string
  kind?: string
  tool?: string | null
  taskId?: string | null
  timestamp?: string | null
  content?: string | null
  contentLength?: number | null
}

export interface SubmitTaskResponse {
  workspace_id: string
  session_id: string
  source_session: string | null
  status: string
}

export interface DeleteWorkspaceResult {
  workspace_id: string
  container_removed: boolean
  queue_items_removed: number
  status: string
}

export interface ReportDocument {
  title: string
  path?: string | null
  generated: string
  blocks: Array<Record<string, unknown>>
}

export interface LaunchProjectRowInput {
  repo_url: string
  name?: string | null
  ref?: string | null
  goal?: string | null
  record?: boolean
}

export interface LaunchBatchRequestBody {
  concurrency?: number | null
  projects: LaunchProjectRowInput[]
}

export interface LaunchAcceptedRow {
  launch_id: string
  row_index: number
  workspace_id: string
  status: string
}

export interface LaunchRejectedRow {
  row_index: number
  workspace_id: string | null
  status: string
  message: string
}

export interface LaunchBatchResponse {
  batch_id: string | null
  concurrency: number
  accepted: LaunchAcceptedRow[]
  rejected: LaunchRejectedRow[]
}

export interface LaunchBatchResult extends LaunchBatchResponse {
  status: number
}

export interface LaunchQueueSummary {
  queued: number
  launching: number
  running: number
  completed: number
  failed: number
}

export interface LaunchQueueItem {
  id: string
  row_index: number
  repo_url: string
  workspace_id: string
  ref: string | null
  status: string
  pid: number | null
  exit_code: number | null
  error: string | null
  process_log: string
}

export interface LaunchQueueBatch {
  id: string
  status: string
  concurrency: number
  created: string
  items: LaunchQueueItem[]
}

export interface LaunchQueueState {
  default_concurrency: number
  summary: LaunchQueueSummary
  batches: LaunchQueueBatch[]
}
