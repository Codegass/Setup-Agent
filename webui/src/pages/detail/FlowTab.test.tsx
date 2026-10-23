import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, TrajectoryDocument } from "@/api/types"

import { FlowTab, RecordedFlowTab } from "./FlowTab"

afterEach(() => { cleanup(); vi.restoreAllMocks() })

function ledger(overrides: Partial<TrajectoryDocument> = {}): TrajectoryDocument {
  return { schema_version: 1, session: { run_id: "S1" }, phases: [], turns: [], annotations: [], warnings: [], ...overrides }
}

function makeDetail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "S1",
    workspace: "sag-acme",
    title: "t",
    status: "partial",
    entry: "SAG",
    start: "now",
    duration: "8m 01s",
    outcome: "⚠️ PARTIAL",
    build: { state: "success", tool: "maven", time: "2m 41s", note: "" },
    test: { state: "partial", pass: 1186, fail: 7, skip: 12, total: 1205 },
    report: "ready",
    evidence: [],
    logs: [],
    context: {
      trunk: {
        goal: "Setup and configure the acme-platform project to be runnable",
        state: "partial",
        progress: { done: 4, total: 5 },
        summary: "Cloned, provisioned JDK 17, built the reactor, ran tests (partial).",
      },
      phases: [
        {
          id: "P1",
          name: "provision",
          title: "Clone + toolchain",
          status: "completed",
          refs: [],
          progress: { iterations: 3, actions: 3 },
          tasks: [
            {
              id: "T1",
              title: "Clone the repo and provision the JDK",
              status: "completed",
              iterations: [
                {
                  iteration: 1,
                  sequence: 1,
                  thoughts: [
                    "The repo is a Maven reactor with 4 modules; it needs JDK 17.",
                  ],
                  actions: [
                    {
                      toolName: "project.clone",
                      success: true,
                      output: "$ git clone acme-platform\nCloning into 'acme-platform'...",
                      observation: "Repository cloned successfully.",
                      refs: [],
                      dispatchStatus: null,
                    },
                  ],
                },
              ],
            },
          ],
        },
      ],
      debug: {},
    },
    ...overrides,
  }
}

describe("FlowTab", () => {
  it("links a chart selection to its iteration, with Advisor identity and exact separate bills", () => {
    const detail = makeDetail()
    const entry = detail.context!.phases[0].tasks[0].iterations[0]
    entry.actions[0].toolName = "advisor"
    const trajectory = ledger({ turns: [{ turn_id: 7, phase: "provision", iteration: 1, actor: "model", call: { tool: "advisor" }, tokens: { input: 100, output: 20 }, advisor_tokens: { input: 3000, output: 1000 }, advisor: { model: "openai/gpt-5.4-mini", effort: "high", request_refs: ["output_request"] }, t0: "2026-10-21T12:00:00Z", t1: "2026-10-21T12:00:10Z", control_seq: [1] }] })
    const scroll = vi.fn()
    const previous = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    const { container } = render(<FlowTab detail={detail} trajectory={trajectory} />)
    const row = screen.getByTestId("turn-row-7")
    expect(row).toHaveClass("bg-advisor-soft")
    expect(within(row).getByText("openai/gpt-5.4-mini")).toBeInTheDocument()
    expect(within(row).getByText(/high effort/)).toBeInTheDocument()
    const chart = within(screen.getByRole("group", { name: "Advisor tokens per consult" }))
    fireEvent.keyDown(chart.getByRole("button", { name: /Show turn 7/ }), { key: "Enter" })
    expect(container.querySelector('[data-flow-iteration="1"]')).toHaveFocus()
    expect(scroll).toHaveBeenCalledWith({ block: "center" })
    expect(screen.getByRole("button", { name: "Back to usage chart" })).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText("Iteration usage"))
    const table = screen.getByRole("table")
    expect(within(table).getByText("100")).toBeInTheDocument()
    expect(within(table).getByText("20")).toBeInTheDocument()
    expect(within(table).getByText("3,000")).toBeInTheDocument()
    expect(within(table).getByText("1,000")).toBeInTheDocument()
    Element.prototype.scrollIntoView = previous
  })

  it("preserves ledger calls when the context trace is unavailable", () => {
    render(<FlowTab detail={makeDetail({ context: null })} trajectory={ledger({ turns: [{ turn_id: 1, phase: "build", iteration: 1, actor: "controller", call: { tool: "advisor" }, control_seq: [] }] })} />)
    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
    expect(screen.getByText("Model not recorded")).toBeInTheDocument()
    expect(screen.getByText(/Effort not recorded/)).toBeInTheDocument()
    expect(screen.queryByText("Flow is not available for this run.")).not.toBeInTheDocument()
  })

  it("renders the goal and a short phase name without treating completion as success", () => {
    render(<FlowTab detail={makeDetail()} />)
    expect(
      screen.getByText("Setup and configure the acme-platform project to be runnable"),
    ).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "provision" })).toBeInTheDocument()
    expect(screen.getByText("ended")).toHaveClass("text-muted-foreground")
  })

  it("renders the think thought (italic reasoning) and the action tool name", () => {
    render(<FlowTab detail={makeDetail()} />)
    expect(
      screen.getByText(/The repo is a Maven reactor with 4 modules/),
    ).toBeInTheDocument()
    expect(screen.getByText("project.clone")).toBeInTheDocument()
  })

  it("opens the action detail modal when an action row is clicked", () => {
    render(<FlowTab detail={makeDetail()} />)
    expect(screen.queryByText(/raw tool result/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText("project.clone"))
    expect(screen.getByText(/raw tool result/i)).toBeInTheDocument()
    // The agent's interpretation is unique to the modal (the row shows it as a
    // truncated "↳ ..." preview prefixed with the arrow, not the bare text).
    expect(screen.getByText("Repository cloned successfully.")).toBeInTheDocument()
  })

  it("shows a failed status on an action row when the action did not succeed", () => {
    const base = makeDetail()
    const ctx = base.context!
    const action = ctx.phases[0].tasks[0].iterations[0].actions[0]
    render(
      <FlowTab
        detail={makeDetail({
          context: {
            ...ctx,
            phases: [
              {
                ...ctx.phases[0],
                tasks: [
                  {
                    ...ctx.phases[0].tasks[0],
                    iterations: [
                      {
                        ...ctx.phases[0].tasks[0].iterations[0],
                        actions: [{ ...action, success: false }],
                      },
                    ],
                  },
                ],
              },
            ],
          },
        })}
      />,
    )
    expect(screen.getByText("failed")).toBeInTheDocument()
  })

  it("renders completed phase progress independently of the run outcome", () => {
    render(
      <FlowTab
        detail={makeDetail({
          context: {
            trunk: { goal: "g", state: "completed", progress: { done: 5, total: 5 }, summary: "" },
            phases: [],
            debug: {},
          },
        })}
      />,
    )
    const stateLabel = screen.getByText("Flow completed")
    expect(stateLabel).toHaveClass("text-muted-foreground")
    expect(screen.getByRole("progressbar", { name: "Phase progress" })).toHaveAttribute("aria-valuenow", "100")
  })

  it("does not turn an unrecorded action result into a failure", () => {
    const detail = makeDetail()
    detail.context!.phases[0].tasks[0].iterations[0].actions[0].success = null
    render(<FlowTab detail={detail} />)
    expect(screen.getByText("not recorded")).toHaveClass("text-muted-foreground")
    expect(screen.queryByText("failed")).not.toBeInTheDocument()
  })

  it("returns keyboard focus to the action after closing its details", async () => {
    render(<FlowTab detail={makeDetail()} />)
    const action = screen.getByRole("button", { name: /project.clone/ })
    action.focus()
    fireEvent.click(action)
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    await waitFor(() => expect(action).toHaveFocus())
  })

  it("does not infer reasoning from an empty entry", () => {
    const detail = makeDetail()
    const entry = detail.context!.phases[0].tasks[0].iterations[0]
    entry.thoughts = []
    entry.actions = []
    render(<FlowTab detail={detail} />)
    expect(screen.getByText("No tool calls or notes were recorded for this entry.")).toBeInTheDocument()
    expect(screen.queryByText(/reasoning step/i)).not.toBeInTheDocument()
  })

  it("shows an honest empty state when no trace was supplied", () => {
    render(<FlowTab detail={makeDetail({ context: null })} />)
    expect(screen.getByText("Flow is not available for this run.")).toBeInTheDocument()
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument()
  })

  it("falls back to an empty state when no phases are recorded", () => {
    render(
      <FlowTab
        detail={makeDetail({
          context: {
            trunk: { goal: "g", state: "unknown", progress: {}, summary: "" },
            phases: [],
            debug: {},
          },
        })}
      />,
    )
    expect(screen.getByText(/no phases/i)).toBeInTheDocument()
  })

  it("keeps a completed workflow partial when its result and final gate are partial (FreeMarker)", () => {
    const detail = makeDetail()
    detail.context!.trunk.state = "completed"
    detail.context!.phases[0].name = "test"
    detail.resultCard = { verdict: "partial", stats: { phasesCompleted: 5, phasesTotal: 5 } } as never
    render(<FlowTab detail={detail} trajectory={ledger({ phases: [{ name: "test", termination: "advance", gates: [{ word: "partial" }] }] })} />)
    expect(screen.getByLabelText("Result: partial")).toHaveClass("text-status-attention")
    expect(within(screen.getByRole("region", { name: "Phase test" })).getByText("partial")).toHaveClass("text-status-attention")
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "5 / 5 phases")
    expect(screen.queryByText("completed")).not.toBeInTheDocument()
  })

  it("does not call a skipped planned phase failed, and counts only entered phases (DBCP)", () => {
    const detail = makeDetail()
    detail.context!.phases.push({ id: "test", name: "test", title: "Test", status: "failed", keyResults: "[skipped] build_not_ready", tasks: [], progress: {}, refs: [] })
    detail.resultCard = { verdict: "failed", stats: { phasesCompleted: 3, phasesTotal: 4 } } as never
    render(<FlowTab detail={detail} trajectory={ledger({ phases: [{ name: "provision", termination: "advance", gates: [{ word: "success" }] }] })} />)
    const phase = within(screen.getByRole("region", { name: "Phase test" }))
    expect(phase.getByText("not run")).toHaveClass("text-muted-foreground")
    expect(phase.queryByText("failed")).not.toBeInTheDocument()
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "3 / 4 phases")
    expect(screen.getByLabelText("Result: failed")).toHaveClass("text-status-failed")
  })

  it("keeps a phase pending while a live run has not reached it", () => {
    const detail = makeDetail({ status: "running" })
    detail.context!.phases[0].tasks = []
    render(<FlowTab detail={detail} trajectory={ledger({ phases: [{ name: "analyze", gates: [] }] })} />)
    expect(screen.getByText("pending")).toBeInTheDocument()
    expect(screen.queryByText("not run")).not.toBeInTheDocument()
  })

  it("preserves calls when the ledger lacks a phase, and does not infer a skip from missing evidence", () => {
    const detail = makeDetail()
    detail.context!.phases.push({ id: "test", name: "test", title: "Test", status: "failed", tasks: [], progress: {}, refs: [] })
    render(<FlowTab detail={detail} trajectory={ledger({ phases: [{ name: "analyze", gates: [] }] })} />)
    expect(screen.getByText("not graded")).toBeInTheDocument()
    expect(screen.getByText("not recorded")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /project.clone/ })).toBeInTheDocument()
    expect(screen.queryByText("not run")).not.toBeInTheDocument()
  })

  it("draws ledger usage and reads a ref omitted from the bounded context only on demand (Gson)", async () => {
    const detail = makeDetail()
    const action = detail.context!.phases[0].tasks[0].iterations[0].actions[0]
    action.refs = [{ ref: "out-build", label: "build output" }, { ref: "out-check", label: "check output" }]
    action.parameters = { command: "mvn verify" }
    const doc = ledger({
      phases: [{ name: "provision", termination: "advance", gates: [{ word: "success" }] }],
      turns: [{ turn_id: 1, phase: "provision", iteration: 1, actor: "model", call: { tool: "project.clone" }, tokens: { input: 100, output: 20 }, advisor_tokens: { input: 30, output: 4 }, t0: "2026-10-21T12:00:00Z", t1: "2026-10-21T12:00:02Z", control_seq: [1, 2] }],
    })
    const response = (payload: unknown) => new Response(JSON.stringify(payload), { status: 200 })
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(doc))
      .mockResolvedValueOnce(response({ ref: "out-build", content: "Full build output" }))
      .mockResolvedValueOnce(response({ ref: "out-check", content: "Full check output" }))
    render(<RecordedFlowTab detail={detail} />)
    expect(await screen.findByRole("group", { name: "Model-response tokens per turn" })).toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Advisor tokens per consult" })).toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Action duration per turn" })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "project.clone · success · details" }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/mvn verify/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /open full output.*build output/ }))
    expect(await screen.findByText("Full build output")).toBeInTheDocument()
    expect(fetchMock).toHaveBeenLastCalledWith("/api/sessions/S1/outputs/out-build")
    fireEvent.click(screen.getByRole("button", { name: /open full output.*check output/ }))
    expect(await screen.findByText("Full check output")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /open full output.*build output/ }))
    expect(screen.getByText("Full build output")).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })
})
