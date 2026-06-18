// Shared, realistic fixtures for the authored previews. Imported by the
// preview .tsx files (esbuild bundles it; the .ts extension keeps it out of the
// preview/stale scan). Not a component — never rendered directly.
//
// Scenario: a multi-module Maven project "acme-platform" whose setup finished
// PARTIAL — build succeeded, but a couple of modules have failing tests. This
// is the canonical "needs attention" workbench story.

export const build = {
  state: "success",
  tool: "maven",
  system: "maven",
  time: "2m 41s",
  note: "mvn -B -T1C verify",
  artifact: "acme-core/target/acme-core-2.3.0.jar",
  classCount: 1840,
  jarCount: 4,
  moduleOutputCount: 4,
  artifactSamples: [
    "acme-core/target/acme-core-2.3.0.jar",
    "acme-api/target/acme-api-2.3.0.jar",
  ],
  warnings: ["3 deprecation warnings in acme-cli"],
  evidenceRefs: [],
}

export const test = {
  state: "partial",
  pass: 1186,
  fail: 7,
  skip: 12,
  total: 1205,
  errors: 0,
  passRate: 98.4,
  executionRate: 99.0,
  uniqueTotal: 1190,
  methodExecutionRate: 96.2,
  failingNames: [
    "com.acme.cli.ArgsTest.parsesQuotedValues",
    "com.acme.cli.ArgsTest.rejectsUnknownFlag",
    "com.acme.web.RouterTest.matchesWildcard",
  ],
  note: "surefire across 3 modules",
}

export const failingNames = test.failingNames

export const modules = [
  {
    name: "acme-core", path: "acme-core", buildStatus: "success", buildSource: "reactor",
    classCount: 720, jarCount: 1, testsTotal: 542, testsPassed: 540, testsFailed: 0, testsSkipped: 2,
    testSource: "runner_xml", lineRate: 86.4, branchRate: 74.1, coverageSource: "jacoco",
  },
  {
    name: "acme-api", path: "acme-api", buildStatus: "success", buildSource: "reactor",
    classCount: 410, jarCount: 1, testsTotal: 300, testsPassed: 300, testsFailed: 0, testsSkipped: 0,
    testSource: "runner_xml", lineRate: 81.0, branchRate: 69.2, coverageSource: "jacoco",
  },
  {
    name: "acme-web", path: "acme-web", buildStatus: "success", buildSource: "reactor",
    classCount: 560, jarCount: 1, testsTotal: 284, testsPassed: 279, testsFailed: 1, testsSkipped: 4,
    testSource: "runner_xml", failingNames: ["com.acme.web.RouterTest.matchesWildcard"], failingCount: 1,
    lineRate: 72.5, branchRate: 58.0, coverageSource: "jacoco",
  },
  {
    name: "acme-cli", path: "acme-cli", buildStatus: "failure", buildSource: "reactor",
    classCount: 150, jarCount: 1, testsTotal: 85, testsPassed: 67, testsFailed: 6, testsSkipped: 6,
    testSource: "runner_xml", buildWarnings: 3,
    failingNames: ["com.acme.cli.ArgsTest.parsesQuotedValues", "com.acme.cli.ArgsTest.rejectsUnknownFlag"],
    failingCount: 2, lineRate: 64.2, branchRate: 49.5, coverageSource: "jacoco",
  },
]

export const moduleSummary = {
  modulesTotal: 4, modulesBuilt: 3, modulesFailed: 1, modulesSkipped: 0,
  modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false,
  lineCovered: 4120, lineTotal: 5200, lineRate: 79.2,
  branchCovered: 610, branchTotal: 900, branchRate: 67.8, coverageSource: "jacoco",
}

export const evidence = [
  {
    source: "build", status: "success", counts: "3 ok", time: "14:38:12",
    summary: "Reactor built 3/4 modules; artifacts verified.",
    records: [
      { time: "14:38:12", status: "success", title: "mvn -B -T1C verify", detail: "BUILD SUCCESS in 2m 41s", ref: "build_command" },
      { time: "14:38:13", status: "success", title: "Artifact present", detail: "acme-core-2.3.0.jar (1.2 MB)", ref: "artifact" },
    ],
  },
  {
    source: "test", status: "partial", counts: "1186/1205", time: "14:40:55",
    summary: "Surefire reports parsed; 7 failing across 2 modules.",
    records: [
      { time: "14:40:55", status: "failed", title: "ArgsTest.parsesQuotedValues", detail: "expected \"a b\" but was \"a", ref: "surefire:acme-cli" },
      { time: "14:40:55", status: "failed", title: "RouterTest.matchesWildcard", detail: "no route matched /a/*/c", ref: "surefire:acme-web" },
    ],
  },
]

export const files = {
  snapshot: { base: "a1b2c3d", head: "9f8e7d6", mode: "git diff" },
  counts: { modified: 5, added: 2, deleted: 1, renamed: 0 },
  items: [
    { path: "acme-cli/pom.xml", change: "modified", type: "xml", size: "2.1 KB", mtime: "14:31", note: "added exec-maven-plugin" },
    { path: "acme-core/src/main/java/com/acme/Core.java", change: "modified", type: "java", size: "8.4 KB", mtime: "14:29", note: "" },
    { path: ".sdkmanrc", change: "added", type: "config", size: "42 B", mtime: "14:20", note: "pinned JDK 17" },
    { path: "settings.xml", change: "added", type: "xml", size: "1.1 KB", mtime: "14:21", note: "mirror config" },
    { path: "acme-legacy/Old.java", change: "deleted", type: "java", size: "—", mtime: "14:25", note: "" },
  ],
}

export const reportDoc = {
  title: "acme-platform — setup report",
  path: "setup-report-20260618-144210.md",
  generated: "2026-06-18 14:42:10",
  blocks: [
    { type: "summary", heading: "Outcome", body: "Build succeeded on all reachable modules. 7 tests fail in acme-cli and acme-web; treat the setup as partial." },
    { type: "status", ok: false, text: "Verdict: PARTIAL — 7 failing tests across 2 modules" },
    { type: "h2", text: "Toolchain" },
    { type: "ul", items: ["JDK 17 (Temurin)", "Maven 3.9.6", "JaCoCo 0.8.12"] },
    { type: "h2", text: "Modules" },
    { type: "table", rows: [["Module", "Build", "Tests"], ["acme-core", "success", "540 / 542"], ["acme-cli", "failure", "67 / 85"]] },
    { type: "meta", text: "command: mvn -B -T1C verify" },
  ],
}

export const logs = [
  "14:34:09 | INFO | Session logging initialized. Session ID: 20260618_143409",
  "14:34:09 | INFO | Creating container sag-acme with image ubuntu:24.04",
  "14:34:22 | INFO | Installing essential packages: curl wget git build-essential",
  "14:36:01 | INFO | Provisioned JDK 17 (Temurin) via sdkman",
  "14:38:45 | INFO | mvn -B -T1C verify → BUILD SUCCESS in 2m 41s",
  "14:40:55 | WARNING | 7 test failures across acme-cli, acme-web (maven.test.failure.ignore=true)",
  "14:42:10 | INFO | Final setup report generated.",
]

export const context = {
  trunk: {
    goal: "Setup and configure the acme-platform project to be runnable",
    state: "partial",
    progress: { done: 4, total: 5 },
    summary: "Cloned, provisioned JDK 17, built the reactor, ran tests (partial).",
  },
  phases: [
    {
      id: "phase_provision", name: "provision", title: "Clone + toolchain", status: "completed",
      progress: { iterations: 3, actions: 4 }, refs: [],
      tasks: [{
        id: "t1", title: "Clone the repo and provision the JDK", status: "completed",
        iterations: [{
          sequence: 1, thoughts: ["Repo is a Maven reactor; needs JDK 17."],
          actions: [{ toolName: "project", success: true, output: "cloned acme-platform", observation: "4 modules detected", refs: [] }],
        }],
      }],
    },
    {
      id: "phase_build", name: "build", title: "Compile + test", status: "failed",
      progress: { iterations: 5, actions: 7 }, refs: [],
      tasks: [{
        id: "t2", title: "Build and test the reactor", status: "failed",
        iterations: [{
          sequence: 1, thoughts: ["Run verify with test failures ignored to capture the full picture."],
          actions: [{ toolName: "build", success: false, output: "BUILD SUCCESS; 7 tests failed", observation: "acme-cli, acme-web have failures", refs: [] }],
        }],
      }],
    },
  ],
  debug: {},
}

export const detail = {
  id: "SETUP-acme-20260618-143409",
  workspace: "sag-acme",
  title: "acme-platform setup",
  status: "partial",
  entry: "sag project https://github.com/acme/acme-platform",
  start: "2026-06-18 14:34:09",
  duration: "8m 01s",
  outcome: "⚠️ PARTIAL — build ok, tests partial",
  evidenceStatus: "partial",
  build,
  test,
  modules,
  moduleSummary,
  report: "ready",
  reportDoc,
  blocker: null,
  evidence,
  files,
  context,
  logs,
  partial: false,
}

export const workspaces = [
  {
    id: "sag-acme", project: "acme-platform", container: "sag-acme", stack: "maven",
    tag: null, release: null, commit: "9f8e7d6", docker: { status: "running" },
    task: "setup", build, test, evidenceStatus: "partial", report: "ready", changed: 8,
    activeSession: null, latestSession: "SETUP-acme-20260618-143409", updated: "2m ago",
  },
  {
    id: "sag-commons-cli", project: "commons-cli", container: "sag-commons-cli", stack: "maven",
    tag: "rel/commons-cli-1.7.0", release: null, commit: "c0ffee1", docker: { status: "running" },
    task: "setup", build: "success",
    test: { state: "success", pass: 320, fail: 0, skip: 0, total: 320, passRate: 100 },
    evidenceStatus: "complete", report: "ready", changed: 3,
    activeSession: null, latestSession: "SETUP-commons-cli-20260617", updated: "1h ago",
  },
  {
    id: "sag-bigtop", project: "bigtop", container: "sag-bigtop", stack: "maven",
    tag: null, release: null, commit: "deadbee", docker: { status: "running" },
    task: "setup", build: "fail",
    test: { state: "fail", pass: 0, fail: 0, total: 0 },
    evidenceStatus: "partial", report: "ready", changed: 0,
    activeSession: null, latestSession: "SETUP-bigtop-20260618", updated: "5m ago",
  },
]
