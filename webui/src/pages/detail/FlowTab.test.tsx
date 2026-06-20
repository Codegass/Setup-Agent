import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { FlowTab } from "./FlowTab"

afterEach(() => cleanup())

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
  it("renders the trunk goal and the phase header with a completed badge", () => {
    render(<FlowTab detail={makeDetail()} />)
    expect(
      screen.getByText("Setup and configure the acme-platform project to be runnable"),
    ).toBeInTheDocument()
    expect(screen.getByText("Clone + toolchain")).toBeInTheDocument()
    expect(screen.getByText(/completed/i)).toBeInTheDocument()
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

  it("renders the trunk header in a success tone when the run succeeded", () => {
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
    const stateLabel = screen.getByText("completed")
    expect(stateLabel).toHaveClass("text-status-success")
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
})
