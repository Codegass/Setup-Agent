import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ContextTrace as ContextTraceModel } from "@/api/types"

import { ContextTrace } from "./ContextTrace"

const context: ContextTraceModel = {
  trunk: {
    goal: "Set up commons-lang",
    state: "completed",
    progress: { done: 4, total: 5 },
    summary: "",
  },
  phases: [
    {
      id: "phase_build",
      name: "build",
      title: "Build the project",
      status: "completed",
      notes: "",
      keyResults: "Compilation succeeded.",
      evidenceStatus: "success",
      evidenceRefs: [],
      conflicts: [],
      refs: [
        {
          ref: "output_build_success",
          label: "output_build_success",
          content: "Full build output\nBUILD SUCCESS\nTail line",
          contentLength: 41,
          tool: "build",
        },
      ],
      progress: { iterations: 2, thoughts: 1, actions: 1 },
      tasks: [
        {
          id: "phase_build/work",
          title: "Build the project",
          status: "completed",
          iterations: [
            {
              iteration: 11,
              sequence: 1,
              thoughts: ["Need to compile with the registered build tool."],
              actions: [],
              window: {
                totalChars: 3000,
                stepSpan: 1,
                segments: { intro: 120, ledger: 0, steps: 1 },
                delta: { added: 1, compacted: 0 },
                introText: "=== PHASE: BUILD ===",
              },
            },
            {
              iteration: 12,
              sequence: 2,
              thoughts: [],
              actions: [
                {
                  toolName: "build",
                  success: true,
                  parameters: { action: "compile" },
                  output: "Full output ref: output_build_success",
                  observation: "build succeeded",
                  refs: [
                    {
                      ref: "output_build_success",
                      label: "output_build_success",
                      content: "Full build output\nBUILD SUCCESS\nTail line",
                      contentLength: 41,
                      tool: "build",
                    },
                  ],
                },
              ],
            },
          ],
        },
      ],
    },
  ],
  debug: {},
}

describe("ContextTrace", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders trunk progress and phase rows", () => {
    render(<ContextTrace ctx={context} />)

    expect(screen.getByText("Done / Total")).toBeInTheDocument()
    expect(screen.getByText("4 / 5")).toBeInTheDocument()
    expect(screen.getAllByRole("progressbar")).toHaveLength(1)
    expect(screen.getByText("Trunk - Command Center")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /phase_build build the project/i })).toBeInTheDocument()
  })

  it("expands phase tasks and iterations", () => {
    render(<ContextTrace ctx={context} />)

    fireEvent.click(screen.getByRole("button", { name: /phase_build build the project/i }))
    fireEvent.click(screen.getByRole("button", { name: /phase_build\/work build the project/i }))

    expect(screen.getByText("Compilation succeeded.")).toBeInTheDocument()
    expect(screen.getByText("iter 11")).toBeInTheDocument()
    expect(screen.getByText("iter 12")).toBeInTheDocument()
    expect(screen.getByText("Need to compile with the registered build tool.")).toBeInTheDocument()
    expect(screen.getByText("build succeeded")).toBeInTheDocument()
    expect(screen.getByText(/"action": "compile"/)).toBeInTheDocument()
  })

  it("opens a full output preview from an action ref", () => {
    render(<ContextTrace ctx={context} />)

    fireEvent.click(screen.getByRole("button", { name: /phase_build build the project/i }))
    fireEvent.click(screen.getByRole("button", { name: /phase_build\/work build the project/i }))
    fireEvent.click(screen.getAllByRole("button", { name: "output_build_success" })[0])

    expect(screen.getByText("Output preview")).toBeInTheDocument()
    expect(screen.getByText(/Full build output/)).toBeInTheDocument()
    expect(screen.getByText(/Tail line/)).toBeInTheDocument()
  })
})
