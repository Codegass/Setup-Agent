import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, ResultCard, ResultStats, WorkspaceSummary } from "@/api/types"

import { DetailHeader } from "./DetailHeader"

/** A card carrying only the stats the header reads. The rest of the card is
 *  the result band's business and nothing in this file looks at it. */
function cardWith(stats: ResultStats): ResultCard {
  return {
    schemaVersion: 1,
    runId: "SETUP-commons-cli-20260917-183204",
    verdict: "success",
    verdictSource: "snapshot",
    rows: [],
    stats,
    attention: [],
    notes: [],
  }
}

const workspace: WorkspaceSummary = {
  id: "sag-acme",
  project: "acme-platform",
  container: "sag-acme",
  stack: "maven",
  commit: "9f8e7d6",
  docker: { status: "running", image: "sag/base" },
  task: "Build and test",
  build: { state: "success", tool: "Maven", time: "1s", note: "" },
  test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
  report: "ready",
  changed: 0,
  updated: "2m ago",
  sessions: [
    { id: "CC-1", workspace: "sag-acme", title: "first", status: "completed", entry: "SAG", start: "", duration: "", build: "success", test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 }, report: "ready", files: 0, evidence: 0 },
    { id: "CC-2", workspace: "sag-acme", title: "second", status: "running", entry: "SAG", start: "", duration: "", build: "pending", test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 }, report: "none", files: 0, evidence: 0 },
  ],
}

const noopHandlers = { onSession: () => {}, onNewTask: () => {}, onTerminal: () => {}, onSettings: () => {}, onDelete: () => {} }

function makeDetail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-acme",
    title: "first",
    status: "running",
    entry: "setup",
    start: "now",
    duration: "8m 01s",
    outcome: "Working.",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

describe("DetailHeader", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders the project heading and the entry tag", () => {
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} />)
    expect(screen.getByRole("heading", { name: "acme-platform" })).toBeInTheDocument()
    expect(screen.getByText("setup")).toBeInTheDocument()
  })

  it("names the two models the card names, and the steps", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform", stack: "maven", commit: "9f8e7d6" } as WorkspaceSummary}
        detail={{
          steps: 6,
          stepBudget: 40,
          duration: "8m 01s",
          resultCard: cardWith({ model: "claude-sonnet-4.5", advisorModel: "claude-haiku-4.5" }),
        } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText(/model claude-sonnet-4\.5/)).toBeInTheDocument()
    expect(screen.getByText(/advisor claude-haiku-4\.5/)).toBeInTheDocument()
    expect(screen.getByText(/6\s*\/\s*40 steps/)).toBeInTheDocument()
  })

  it("reads the archived run's line off the card, not the container or the old pin", () => {
    // The line this replaces, seen on the workspace page for the archived
    // commons-cli run: the docker container's name, then a legacy pin string
    // spelling "thinking", then a duration the result card's own Setup row an
    // inch below it disagreed with.
    render(
      <DetailHeader
        workspace={{
          id: "sag-commons-cli",
          project: "commons-cli",
          container: "sag-commons-cli",
          stack: "maven",
          commit: "e171117",
        } as WorkspaceSummary}
        detail={{
          model: "thinking=gpt-5.4-mini;action=gpt-5.4-mini",
          duration: "6m 08s",
          finish: "2026-09-17 18:38:14",
          resultCard: cardWith({
            model: "gpt-5.4-mini",
            advisorModel: "gpt-5.4-mini",
            wallClockSeconds: 368.4298,
          }),
        } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )

    expect(
      screen.getByText(
        "maven · e171117 · model gpt-5.4-mini · advisor gpt-5.4-mini · 6m 08s · finished 2026-09-17 18:38:14",
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/sag-commons-cli/)).toBeNull()
    expect(screen.queryByText(/thinking=/)).toBeNull()
  })

  it("names no advisor when the card names none", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform" } as WorkspaceSummary}
        detail={{
          resultCard: cardWith({ model: "gpt-5.4-mini", wallClockSeconds: 41.04 }),
        } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText("model gpt-5.4-mini · 41.0s")).toBeInTheDocument()
    expect(screen.queryByText(/advisor/)).toBeNull()
  })

  it("names no model at all for a run that recorded no card", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform", stack: "maven" } as WorkspaceSummary}
        detail={{ model: "thinking=x;action=y", duration: "8m 01s" } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText("maven · 8m 01s")).toBeInTheDocument()
  })

  it("falls back to a bare step count when no budget is present", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform" } as WorkspaceSummary}
        detail={{ steps: 6 } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText(/\b6 steps\b/)).toBeInTheDocument()
    expect(screen.queryByText(/\/\s*\d+\s*steps/)).not.toBeInTheDocument()
  })

  it("omits placeholder metadata and uses the selected session finish time", () => {
    render(
      <DetailHeader
        workspace={{
          id: "sag-acme", project: "acme-platform", container: "sag-acme",
          stack: "Unknown", updated: "a newer workspace run",
        } as WorkspaceSummary}
        detail={{
          duration: "—", model: "unknown", finish: "2026-08-16T03:50:48Z",
        } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )

    expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/a newer workspace run/i)).not.toBeInTheDocument()
    expect(screen.getByText(/finished 2026-08-16T03:50:48Z/i)).toBeInTheDocument()
  })

  it("renders the primary New task action and labeled secondary actions", () => {
    const onNewTask = vi.fn()
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} onNewTask={onNewTask} />)
    fireEvent.click(screen.getByRole("button", { name: /new task/i }))
    expect(onNewTask).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("button", { name: /terminal/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /settings/i })).toBeInTheDocument()
  })

  it("opens the overflow menu and exposes Delete + the session switcher", () => {
    const onDelete = vi.fn()
    const onSession = vi.fn()
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} onDelete={onDelete} onSession={onSession} />)

    // Menu is closed initially.
    expect(screen.queryByRole("menuitem", { name: /delete/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }))
    expect(onDelete).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    fireEvent.click(screen.getByRole("menuitemradio", { name: /CC-2/ }))
    expect(onSession).toHaveBeenCalledWith("CC-2")
  })

  it("hides the session switcher in the menu when there is a single session", () => {
    render(
      <DetailHeader
        workspace={{ ...workspace, sessions: [workspace.sessions![0]] }}
        detail={makeDetail()}
        sessionId="CC-1"
        {...noopHandlers}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    expect(screen.queryByRole("menuitemradio", { name: /CC-2/ })).not.toBeInTheDocument()
  })
})
