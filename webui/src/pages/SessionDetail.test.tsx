import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { SessionDetail } from "./SessionDetail"

const detail: ExecutionSessionDetail = {
  id: "CC-3",
  workspace: "sag-commons-cli",
  title: "Build project and execute full test suite",
  status: "running",
  evidenceStatus: "conflict",
  entry: "CLI",
  start: "02:14:08",
  duration: "running · 2m 11s",
  outcome: "Build succeeds and tests are partial.",
  build: {
    state: "success",
    tool: "Maven 3.9.6",
    time: "47.2s",
    artifact: "target/app.jar",
    note: "clean package",
  },
  test: {
    state: "partial",
    pass: 312,
    fail: 8,
    skip: 0,
    total: 320,
    note: "HelpFormatter failures",
  },
  report: "ready",
  reportDoc: {
    title: "setup-report.md",
    generated: "now",
    blocks: [
      { type: "h1", text: "Setup report" },
      { type: "p", text: "Project builds." },
    ],
  },
  evidence: [
    {
      source: "Test validator",
      status: "partial",
      counts: "312 / 320",
      time: "02:16",
      summary: "8 failed",
      records: [
        {
          time: "02:16:30",
          status: "fail",
          title: "HelpFormatterTest",
          detail: "expected wrapped width 74 but was 80",
          ref: "target/surefire-reports/HelpFormatterTest.xml",
        },
      ],
    },
  ],
  files: null,
  context: null,
  logs: ["mvn clean package", "BUILD SUCCESS"],
}

describe("SessionDetail", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders result-first status", () => {
    render(<SessionDetail detail={detail} onBack={() => {}} onNewTask={() => {}} />)

    expect(screen.getByText("Outcome")).toBeInTheDocument()
    expect(screen.getByText("Build project and execute full test suite")).toBeInTheDocument()
    expect(screen.getByText("312")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Status" })).toBeInTheDocument()
    expect(screen.getAllByText("Flow")).not.toHaveLength(0)
    expect(screen.getAllByText("Evidence status")).not.toHaveLength(0)
    expect(screen.getAllByText("Running")).not.toHaveLength(0)
    expect(screen.getAllByText("Conflict")).not.toHaveLength(0)
  })

  it("opens evidence and report tabs without changing the default status tab", () => {
    render(
      <SessionDetail
        detail={detail}
        initialTab="Report"
        onBack={() => {}}
        onNewTask={() => {}}
      />,
    )

    expect(screen.getByText("Project builds.")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Evidence" }))

    expect(screen.getByText("Test validator")).toBeInTheDocument()
    expect(screen.getByText("HelpFormatterTest")).toBeInTheDocument()
  })

  it("shows report source paths without linking to unserved workspace files", () => {
    render(
      <SessionDetail
        detail={{
          ...detail,
          reportDoc: {
            title: "setup-report.md",
            generated: "now",
            path: ".setup_agent/reports/setup-report.md",
            blocks: [
              { type: "h1", text: "Setup report" },
              { type: "p", text: "Project builds." },
            ],
          },
        }}
        initialTab="Report"
        onBack={() => {}}
        onNewTask={() => {}}
      />,
    )

    expect(screen.getByText(".setup_agent/reports/setup-report.md")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: /open raw/i })).not.toBeInTheDocument()
  })

  it("renders recovered setup report blocks without raw JSON fallbacks", () => {
    render(
      <SessionDetail
        detail={{
          ...detail,
          reportDoc: {
            title: "setup-report.md",
            generated: "now",
            blocks: [
              { type: "h1", text: "Project Setup Report" },
              { type: "meta", text: "Generated: 2026-06-06 21:35:09" },
              { type: "status", text: "SUCCESS (Build Passed, 97.7% Tests Pass)", ok: true },
              {
                type: "table",
                rows: [
                  ["Repository", "Cloned successfully"],
                  ["Build", "115 classes, 0 JARs"],
                ],
              },
            ],
          },
        }}
        initialTab="Report"
        onBack={() => {}}
        onNewTask={() => {}}
      />,
    )

    expect(screen.getByRole("heading", { name: "Project Setup Report" })).toBeInTheDocument()
    expect(screen.getByText("SUCCESS (Build Passed, 97.7% Tests Pass)")).toBeInTheDocument()
    expect(screen.getByText("115 classes, 0 JARs")).toBeInTheDocument()
    expect(screen.queryByText(/"type":/)).not.toBeInTheDocument()
  })

  it("hides empty active branch focus in completed setup context", () => {
    render(
      <SessionDetail
        detail={{
          ...detail,
          context: {
            trunk: {
              goal: "Setup commons-cli",
              state: "completed",
              progress: { done: 1, total: 1 },
              summary: "",
            },
            tasks: [
              {
                id: "task_1",
                title: "Clone repository",
                status: "completed",
                summary: "",
                refs: [],
                recovered: false,
              },
            ],
            activeBranch: {
              task: "",
              why: "",
              memory: [],
              lastRefs: [],
              pressure: 0,
            },
            debug: {},
          },
        }}
        initialTab="Context"
        onBack={() => {}}
        onNewTask={() => {}}
      />,
    )

    expect(screen.getByText("Trunk - Command Center")).toBeInTheDocument()
    expect(screen.queryByText("Active Branch Focus")).not.toBeInTheDocument()
  })

  it("keeps branch task context details expandable", () => {
    render(
      <SessionDetail
        detail={{
          ...detail,
          context: {
            trunk: {
              goal: "Setup commons-cli",
              state: "partial",
              progress: { done: 1, total: 1 },
              summary: "",
            },
            tasks: [
              {
                id: "task_4",
                title: "Compile project using Maven",
                status: "completed",
                summary: "Previous task: Java 8 verified.\nmaven succeeded: BUILD SUCCESS",
                refs: ["output_build_success"],
                recovered: false,
              },
            ],
            activeBranch: {
              task: "",
              why: "",
              memory: [],
              lastRefs: [],
              pressure: 0,
            },
            debug: {},
          },
        }}
        initialTab="Context"
        onBack={() => {}}
        onNewTask={() => {}}
      />,
    )

    expect(screen.queryByText(/maven succeeded/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /task_4 compile project using maven/i }))

    expect(screen.getByText(/maven succeeded/)).toBeInTheDocument()
    expect(screen.getByText("output_build_success")).toBeInTheDocument()
  })

  it("starts a new task from the current session id", () => {
    const onNewTask = vi.fn()

    render(<SessionDetail detail={detail} onBack={() => {}} onNewTask={onNewTask} />)

    fireEvent.click(screen.getByRole("button", { name: /new task from this/i }))

    expect(onNewTask).toHaveBeenCalledWith("CC-3")
  })
})
