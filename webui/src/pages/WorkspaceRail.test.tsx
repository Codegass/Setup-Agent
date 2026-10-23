import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DashboardResponse, LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { WorkspaceRail } from "./WorkspaceRail"

function queueItem(overrides: Partial<LaunchQueueItem>): LaunchQueueItem {
  return {
    id: "li-1", row_index: 0, repo_url: "https://github.com/a/b.git", workspace_id: "sag-b",
    ref: null, status: "queued", pid: null, exit_code: null, error: null, process_log: "", ...overrides,
  }
}

function queue(items: LaunchQueueItem[]): LaunchQueueState {
  return {
    default_concurrency: 2,
    summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
    batches: [{ id: "batch-1", status: "running", concurrency: 2, created: "now", items }],
  }
}

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x", project: "owner/x", container: "sag-x", stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" }, task: "t",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready", changed: 0, updated: "just now", ...overrides,
  }
}

/** The rail's result cells, as `_workspace_result` builds them: the card's own
 *  words for the Summary, the CI command line and the Official CI row, the
 *  record's own counts. */
function healthy(): WorkspaceSummary {
  return ws({
    id: "sag-healthy",
    project: "owner/healthy",
    result: {
      verdict: "success",
      ciCommand: { status: "passed", headline: "1 of 1 steps passed" },
      task: { status: "complete", completed: 1, required: 1 },
      tests: { executed: 994, passed: 933, failed: 0, errors: 0, skipped: 61 },
      ci: { status: "compared" },
    },
  })
}

/** Red only through its result: build, container and legacy test state are all
 *  clean, so nothing but the new clause can put this row in attention. */
function failing(): WorkspaceSummary {
  return ws({
    id: "sag-failing",
    project: "owner/failing",
    test: { state: "unknown", pass: 0, fail: 0, skip: 0, total: 0 },
    result: {
      verdict: "failed",
      task: { status: "complete", completed: 2, required: 2 },
      tests: { executed: 120, passed: 118, failed: 1, errors: 1, skipped: 0 },
      ci: { status: "compared" },
    },
  })
}

function running(): WorkspaceSummary {
  return ws({
    id: "sag-running",
    project: "owner/running",
    docker: { status: "running", image: "sag/base" },
    result: {
      verdict: "success",
      task: { status: "complete", completed: 1, required: 1 },
      tests: { executed: 10, passed: 10, failed: 0, errors: 0, skipped: 0 },
      ci: { status: "compared" },
    },
  })
}

function taskIncomplete(): WorkspaceSummary {
  return ws({
    id: "sag-task",
    project: "owner/task",
    result: {
      verdict: "partial",
      task: { status: "incomplete", completed: 0, required: 3 },
      tests: { executed: 40, passed: 40, failed: 0, errors: 0, skipped: 0 },
      ci: { status: "compared" },
    },
  })
}

/** The two Official CI words a current card puts in this cell: whether a
 *  comparison with the official CI run could be made. Neither is a finding;
 *  how the run fell short is stated on the card's Official CI row. */
function withCI(id: string, project: string, status: string): WorkspaceSummary {
  return ws({
    id,
    project,
    result: {
      verdict: "success",
      task: { status: "complete", completed: 1, required: 1 },
      tests: { executed: 40, passed: 40, failed: 0, errors: 0, skipped: 0 },
      ci: { status },
    },
  })
}

/** The rail rows in the order they render, by the project each one names. */
function renderedOrder(): string[] {
  return screen
    .getAllByRole("button", { name: /^Open workspace / })
    .map((node) => node.getAttribute("aria-label")!.replace(/^Open workspace /, ""))
}

const data: DashboardResponse = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    ws({ id: "sag-healthy", project: "owner/healthy" }),
    ws({ id: "sag-broken", project: "owner/broken", build: { state: "failure", tool: "Maven", time: "", note: "" } }),
  ],
}

const props = {
  data, selectedId: "sag-healthy", onSelect: () => {}, onLaunchSetups: () => {},
  launchQueue: null, highlightedWorkspaces: [], lastUpdatedAt: Date.now(), pollFailed: false,
}

describe("WorkspaceRail", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders a row per workspace and marks the selected one", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByRole("button", { name: /owner\/healthy/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toHaveAttribute("aria-current", "false")
  })

  it("summarises the fleet in one line, not three cards", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [running(), failing(), healthy()] }}
      />,
    )
    expect(screen.getByText("3 workspaces")).toBeInTheDocument()
    expect(screen.getByText("· 3 running")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /1 needs attention/ })).toBeInTheDocument()
    expect(screen.queryByText("Build coverage")).not.toBeInTheDocument()
    expect(screen.queryByText("Run results")).not.toBeInTheDocument()
    expect(screen.queryByText("Workspaces")).not.toBeInTheDocument()
    expect(screen.queryByText("Containers")).not.toBeInTheDocument()
  })

  it("filters to what needs attention when asked", () => {
    render(
      <WorkspaceRail {...props} data={{ ...data, workspaces: [failing(), healthy()] }} />,
    )
    expect(screen.getByRole("button", { name: /owner\/healthy/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /needs attention/i }))
    expect(screen.queryByRole("button", { name: /owner\/healthy/ })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: /owner\/failing/ })).toBeInTheDocument()
  })

  it("shows each row's four results", () => {
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [healthy()] }} />)
    expect(screen.getByText("success")).toBeInTheDocument()
    // The card's first line takes the required-task cell when the card has one.
    expect(screen.getByTitle("Task verification: 1 of 1 steps passed")).toHaveTextContent("passed")
    expect(screen.queryByText("1/1")).not.toBeInTheDocument()
    expect(screen.getByText("933/994")).toBeInTheDocument()
    expect(screen.getByText("compared")).toBeInTheDocument()
  })

  it("falls back to the required-task fraction when the result has no CI command line", () => {
    const older = healthy()
    older.result = { ...older.result!, ciCommand: null }
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [older] }} />)
    expect(screen.getByText("1/1")).toBeInTheDocument()
  })

  it("marks red results on a row", () => {
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [failing()] }} />)
    expect(screen.getByText("+2")).toBeInTheDocument()
  })

  it("does not frame an optional task or CI baseline as missing for a normal run", () => {
    const ordinary = healthy()
    ordinary.result = { ...ordinary.result!, ciCommand: null, task: null, ci: null }
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [ordinary] }} />)
    expect(screen.getByRole("button", {
      name: "Open workspace owner/healthy — summary success, tests 933/994",
    })).toBeInTheDocument()
    expect(screen.queryByTitle("Required task steps completed")).toBeNull()
    expect(screen.queryByTitle("How this run measures against the project's own CI")).toBeNull()
  })

  it("says so once when a run recorded no result at all", () => {
    render(
      <WorkspaceRail {...props} data={{ ...data, workspaces: [ws({ id: "sag-none", project: "owner/none" })] }} />,
    )
    expect(screen.getByText("no result")).toBeInTheDocument()
    expect(screen.queryByText("—")).not.toBeInTheDocument()
  })

  it("names each result in the row a screen reader hears", () => {
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [healthy()] }} />)
    expect(
      screen.getByRole("button", {
        name: "Open workspace owner/healthy — summary success, task verification passed: 1 of 1 steps passed, tests 933/994, official CI compared",
      }),
    ).toBeInTheDocument()
  })

  it("puts an incomplete required task at the top", () => {
    render(
      <WorkspaceRail {...props} data={{ ...data, workspaces: [healthy(), taskIncomplete()] }} />,
    )
    expect(renderedOrder()[0]).toContain("owner/task")
  })

  it("puts a failed result at the top", () => {
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [healthy(), failing()] }} />)
    expect(renderedOrder()[0]).toContain("owner/failing")
  })

  it("flags a run whose pinned CI command failed", () => {
    // An older record keeps the word it sealed (here `partial`) while its card
    // reads the CI command's own result off the task steps; the command line is
    // what flags it.
    const commandFailed = ws({
      id: "sag-ci",
      project: "owner/ci",
      result: {
        verdict: "partial",
        ciCommand: { status: "failed", headline: "0 of 1 steps passed · step 1 of 1 exited 1" },
        tests: { executed: 40, passed: 40, failed: 0, errors: 0, skipped: 0 },
        ci: { status: "not compared" },
      },
    })
    render(<WorkspaceRail {...props} data={{ ...data, workspaces: [healthy(), commandFailed] }} />)
    expect(renderedOrder()[0]).toContain("owner/ci")
    expect(screen.getByTitle("Task verification: 0 of 1 steps passed · step 1 of 1 exited 1")).toHaveTextContent("failed")
    fireEvent.click(screen.getByRole("button", { name: /needs attention/i }))
    expect(renderedOrder()).toEqual([
      "owner/ci — summary partial, task verification failed: 0 of 1 steps passed · step 1 of 1 exited 1, tests 40/40, official CI not compared",
    ])
  })

  it("leaves a comparison, and one with nothing to compare, unflagged", () => {
    // "not compared" is what the card spells when no official CI numbers could
    // be set beside this run's — no CI record, no matched job, or a job that
    // states no counts. Flagging it would flag most of the fleet, so the rail
    // leaves it to the Official CI row, which has the tone to tell them apart.
    render(
      <WorkspaceRail
        {...props}
        data={{
          ...data,
          workspaces: [
            withCI("sag-compared", "owner/compared", "compared"),
            withCI("sag-none", "owner/none", "not compared"),
          ],
        }}
      />,
    )
    expect(screen.queryByRole("button", { name: /needs attention/i })).not.toBeInTheDocument()
    expect(screen.getByText("compared")).toBeInTheDocument()
    expect(screen.getByText("not compared")).toBeInTheDocument()
  })

  it("does not print placeholder stack metadata", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [ws({ stack: "Unknown", commit: "abc123" })] }}
      />,
    )

    expect(screen.queryByText("Unknown")).not.toBeInTheDocument()
    expect(screen.getByText("abc123")).toBeInTheDocument()
  })

  it("orders attention-needing workspaces first", () => {
    render(<WorkspaceRail {...props} />)
    const rows = screen.getAllByRole("button", { name: /owner\// })
    expect(rows[0].getAttribute("aria-label")).toContain("owner/broken")
  })

  it("selects a workspace when its row is clicked", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
  })

  it("calls onAfterSelect after selecting a workspace (drawer close hook)", () => {
    const onSelect = vi.fn()
    const onAfterSelect = vi.fn()
    render(<WorkspaceRail {...props} onAfterSelect={onAfterSelect} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
    expect(onAfterSelect).toHaveBeenCalled()
  })

  it("renders a workspace in a non-interactive deleting state", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} deletingIds={new Set(["sag-broken"])} onSelect={onSelect} />)
    const row = screen.getByRole("button", { name: /owner\/broken/ })
    expect(row).toBeDisabled()
    expect(screen.getByText(/deleting/i)).toBeInTheDocument()
    fireEvent.click(row)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it("batch-deletes selected workspaces via select mode", async () => {
    const onDeleteMany = vi.fn().mockResolvedValue(undefined)
    render(<WorkspaceRail {...props} onDeleteMany={onDeleteMany} />)
    fireEvent.click(screen.getByRole("button", { name: /^select$/i }))
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/healthy/i }))
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/broken/i }))
    fireEvent.click(screen.getByRole("button", { name: /delete 2 selected/i }))
    fireEvent.click(screen.getByRole("button", { name: /delete 2 workspaces/i }))
    await waitFor(() => expect(onDeleteMany).toHaveBeenCalled())
    expect(onDeleteMany.mock.calls[0][0]).toEqual(
      expect.arrayContaining(["sag-healthy", "sag-broken"]),
    )
  })

  it("filters rows by the query input", () => {
    render(<WorkspaceRail {...props} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: "broken" } })
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /owner\/healthy/ })).not.toBeInTheDocument()
  })

  it("fires the launch action", () => {
    const onLaunchSetups = vi.fn()
    render(<WorkspaceRail {...props} onLaunchSetups={onLaunchSetups} />)
    fireEvent.click(screen.getByRole("button", { name: /launch setups/i }))
    expect(onLaunchSetups).toHaveBeenCalled()
  })

  it("shows the updated stamp in the footer", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByText(/updated just now/i)).toBeInTheDocument()
  })

  it("renders pending launches as muted setting-up rows above workspaces", () => {
    render(
      <WorkspaceRail
        {...props}
        launchQueue={queue([
          queueItem({ id: "q1", workspace_id: "sag-queued", status: "queued" }),
          queueItem({ id: "r1", workspace_id: "sag-launching", status: "launching" }),
        ])}
      />,
    )
    expect(screen.getByText(/waiting for a free setup slot/i)).toBeInTheDocument()
    expect(screen.getByText(/setting up/i)).toBeInTheDocument()
  })

  it("renders a failed launch with its error and a remove button", () => {
    const onRemoveLaunch = vi.fn().mockResolvedValue(undefined)
    render(
      <WorkspaceRail
        {...props}
        launchQueue={queue([
          queueItem({ id: "f1", workspace_id: "sag-fail", status: "failed", error: "clone failed" }),
        ])}
        onRemoveLaunch={onRemoveLaunch}
      />,
    )
    expect(screen.getByText("clone failed")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /remove failed launch fail/i })).toBeInTheDocument()
  })

  it("does not show the empty state while launches are queued", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [] }}
        launchQueue={queue([queueItem({ id: "q1", workspace_id: "sag-queued", status: "queued" })])}
        selectedId={null}
      />,
    )
    expect(screen.queryByText(/no workspaces yet/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/pending launch queued/i)).toBeInTheDocument()
  })

  it("distinguishes an unavailable workspace read from an empty dashboard", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{
          ...data,
          readStatus: "unavailable",
          readError: "workspace registry read failed",
          workspaces: [],
        }}
        selectedId={null}
      />,
    )

    expect(screen.getByRole("alert")).toHaveTextContent("Workspace data unavailable")
    expect(screen.getByRole("button", { name: /retry dashboard/i })).toBeInTheDocument()
    expect(screen.queryByText(/no workspaces yet/i)).not.toBeInTheDocument()
  })
})
