export type Tone = "neutral" | "blue" | "green" | "red" | "amber"
export type CanonicalVerdict = "success" | "partial" | "failed" | "unknown"
export type VerdictSource = "snapshot" | "legacy" | "derived"
export type SnapshotStatus =
  | "valid"
  | "missing"
  | "corrupt"
  | "untrusted"
  | "legacy"
  | "unavailable"
export type ReportDeliveryStatus = "delivered" | "failed" | "skipped"

export interface DockerSummary {
  status: string
  image?: string | null
  version?: string | null
  endpoint?: string | null
}

export interface SystemSummary {
  dockerDiskUsed?: number | null
  dockerReclaimable?: number | null
  memUsed?: number | null
  memTotal?: number | null
  cpuLoad?: number | null
}

export interface EvidenceCountSummary {
  executed: number | null
  passed: number | null
  failed: number | null
  errors: number | null
  skipped: number | null
  availability?: "available" | "unavailable" | null
  reason?: string | null
  basis?: string | null
}

export interface ObservationCountSummary extends EvidenceCountSummary {
  reportFileCount?: number | null
  reasonCounts?: Record<string, number> | null
}

export interface TestEvidenceLayers {
  claimed: {
    latestSubjects: EvidenceCountSummary
    latestCases: EvidenceCountSummary
    receiptExecutions: EvidenceCountSummary
  }
  quarantinedObservations: ObservationCountSummary
  unattributedObservations: ObservationCountSummary
  staleObservations: ObservationCountSummary
  retriedCases?: number | null
  flakyCases?: number | null
}

export interface MetricsV2EvidenceSummary {
  integrity: "complete" | "degraded" | "failed" | "unavailable"
  receiptsExpected: number | null
  receiptsPersisted: number | null
  terminalReceiptsUnpersisted: number | null
  conflictCount: number
}

export interface MetricsV2Summary {
  schemaVersion: 2
  identityVersion: "module-qualified-v1"
  run: {
    runId: string | null
    targetSha: string | null
    sagSha: string | null
    promptHash: string | null
    controlBundleHash: string | null
    imageDigest: string | null
    modelPin: string | null
    runOrderIndex: number | null
    pinStatus: "complete" | "incomplete"
    missingPins: string[]
  }
  outcome: {
    verdict: CanonicalVerdict
    buildState: string
    testState: string
    terminalReason: string
  }
  evidence: MetricsV2EvidenceSummary
  tests: TestEvidenceLayers
  coverage: {
    domainsDiscovered: number | null
    domainsAttempted: number | null
    domainsTerminal: number | null
    domainsWithClaimedTests: number | null
  }
  control: {
    terminalRefusalRecurrences: number | null
    unsettledJobs: number | null
    cleanupEscalations: number | null
    midrunHumanApprovals: number | null
  }
}

export interface EvidenceLayerProjectionSummary {
  projectionStatus: "metrics-v2-artifact-unavailable"
  tests: TestEvidenceLayers
  evidence: MetricsV2EvidenceSummary
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
  rawExecutions?: number | null
  declaredTotal?: number | null
  methodExecutionRate?: number | null
  failingNames?: string[]
  conflicts?: string[]
  evidenceRefs?: string[]
  note?: string
  evidenceLayers?: MetricsV2Summary | EvidenceLayerProjectionSummary | null
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
  canonicalVerdict?: CanonicalVerdict
  snapshotStatus?: SnapshotStatus
  legacy?: boolean
  reportDeliveryStatus?: ReportDeliveryStatus | null
}

export interface DashboardResponse {
  docker: DockerSummary
  workspaces: WorkspaceSummary[]
  readStatus?: "available" | "unavailable"
  readError?: string | null
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

export interface ModuleSummary {
  name: string
  path: string
  buildStatus: "success" | "failure" | "skipped" | "unknown"
  buildSource: "reactor" | "artifacts" | "partial" | "none"
  classCount?: number | null
  jarCount?: number | null
  buildWarnings?: number | null
  buildErrorSamples?: string[]
  testsTotal?: number | null
  testsPassed?: number | null
  testsFailed?: number | null
  testsErrors?: number | null
  testsSkipped?: number | null
  testSource: "runner_xml" | "partial" | "none"
  failingNames?: string[]
  failingCount?: number | null
  evidenceRefs?: string[]
  lineCovered?: number | null
  lineTotal?: number | null
  lineRate?: number | null
  branchCovered?: number | null
  branchTotal?: number | null
  branchRate?: number | null
  coverageSource?: string | null
}

export interface ModuleRollup {
  modulesTotal: number
  modulesBuilt: number
  modulesFailed: number
  modulesSkipped: number
  modulesWithTestFailures: number
  buildSystems: string[]
  singleModule: boolean
  lineCovered?: number | null
  lineTotal?: number | null
  lineRate?: number | null
  branchCovered?: number | null
  branchTotal?: number | null
  branchRate?: number | null
  coverageSource?: string | null
}

export interface VerdictSummary {
  tone: "success" | "attention" | "failed"
  headline: string
  detail?: string | null
  verdict?: CanonicalVerdict | null
  source?: VerdictSource
}

export interface ExecutionSessionDetail {
  id: string
  workspace: string
  title: string
  status: string
  entry: string
  start: string
  finish?: string | null
  duration: string
  outcome: string
  verdict?: VerdictSummary | null
  model?: string | null
  steps?: number | null
  stepBudget?: number | null
  evidenceStatus?: string | null
  canonicalVerdict?: CanonicalVerdict
  rates?: Record<string, unknown> | null
  snapshotStatus?: SnapshotStatus
  legacy?: boolean
  reportDeliveryStatus?: ReportDeliveryStatus | null
  build: BuildSummary
  test: TestSummary
  modules?: ModuleSummary[]
  moduleSummary?: ModuleRollup | null
  report: string
  reportDoc?: ReportDocument | null
  blocker?: { code: string; title: string; detail: string; hint: string } | null
  evidence: EvidenceGroup[]
  files?: FileChangeDigest | null
  context?: ContextTrace | null
  logs: string[]
  partial?: boolean
  /** A fabricated session (`sag ui --demo`): it stands for no run, so no
   *  control ledger exists for it and no trajectory can be derived. */
  demo?: boolean
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

// ── trajectory-v1 ────────────────────────────────────────────────────────────
// One vocabulary with `src/sag/trajectory/schema.py`, which is why these fields
// are snake_case where the rest of this file is camelCase: the trajectory
// endpoint serves the reducer's own document unaliased, and renaming it here
// would put a second name on every field a warning or a CLI dump already uses.

export type TrajectoryDetail = "summary" | "full"
export type TrajectoryActor = "model" | "controller"
export type TrajectoryAnnotationKind =
  | "refusal"
  | "forced"
  | "repair_context"
  | "recurrence"
  | "conflict"

export interface TrajectorySession {
  run_id: string
  project?: string | null
  verdict?: string | null
  rates?: Record<string, unknown> | null
  wall_clock_seconds?: number | null
}

/** What was asked of the tool. `params_ref` names a control-ledger envelope,
 *  not an output-store handle. */
export interface TrajectoryCall {
  tool: string
  params_ref?: string | null
}

/** Exact call parameters resolved lazily from one control-ledger envelope. */
export interface TrajectoryEnvelope {
  sequence: number | null
  envelope_id: string
  tool: string
  exact_params: Record<string, unknown>
}

/** The tool result. `ref` is what the model read; `evidence_ref` is the raw
 *  tool output, when those are not the same bytes. */
export interface TrajectoryObservation {
  ref?: string | null
  evidence_ref?: string | null
  error_code?: string | null
  failure_signature?: string | null
}

export interface TrajectoryGate {
  word: string
  decision_id?: string | null
  supersedes?: string | null
}

export interface TrajectoryTokens {
  input: number
  output: number
}

export interface TrajectoryTurn {
  turn_id: number
  phase: string
  iteration?: number | null
  actor: TrajectoryActor
  window_ref?: string | null
  /** Model context in render order. A `window_truncated:<n>` entry names a cut
   *  rather than bytes and resolves to nothing on purpose. */
  window_components?: string[] | null
  call?: TrajectoryCall | null
  observation?: TrajectoryObservation | null
  gate?: TrajectoryGate | null
  tokens?: TrajectoryTokens | null
  t0?: string | null
  t1?: string | null
  control_seq: number[]
}

export interface TrajectoryPhase {
  name: string
  termination?: string | null
  /** Every grading the phase made, superseded ones included — which is what
   *  makes a supersedes chain walkable from the document alone. */
  gates: TrajectoryGate[]
}

export interface TrajectoryAnnotation {
  kind: TrajectoryAnnotationKind
  turn_id: number
  data: Record<string, unknown>
}

export interface TrajectoryWarning {
  code: string
  detail: string
  control_seq?: number | null
  turn_id?: number | null
}

export interface TrajectoryDocument {
  schema_version: number
  session: TrajectorySession
  phases: TrajectoryPhase[]
  turns: TrajectoryTurn[]
  annotations: TrajectoryAnnotation[]
  warnings: TrajectoryWarning[]
  /** Full tier only: every resolvable ref mapped to its verbatim bytes. `null`
   *  means the tier was not asked for; `{}` means it was and nothing resolved. */
  outputs?: Record<string, string> | null
}

export interface LaunchProjectRowInput {
  repo_url: string
  name?: string | null
  ref?: string | null
  goal?: string | null
  record?: boolean
  coverage?: boolean
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
