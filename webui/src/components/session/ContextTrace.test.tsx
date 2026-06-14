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
    expect(screen.getByText("Trunk goal")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /phase_build build the project/i })).toBeInTheDocument()
  })

  it("expands a phase straight to its iteration timeline", () => {
    render(<ContextTrace ctx={context} />)

    // A single-task phase flattens: one click reaches the iterations.
    fireEvent.click(screen.getByRole("button", { name: /phase_build build the project/i }))

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
    fireEvent.click(screen.getAllByRole("button", { name: "output_build_success" })[0])

    expect(screen.getByText("Output preview")).toBeInTheDocument()
    expect(screen.getByText(/Full build output/)).toBeInTheDocument()
    expect(screen.getByText(/Tail line/)).toBeInTheDocument()
  })

  it("explains iterations that only have journal window metadata", () => {
    const sparseContext: ContextTraceModel = {
      ...context,
      phases: [
        {
          ...context.phases[0],
          tasks: [
            {
              ...context.phases[0].tasks[0],
              iterations: [
                {
                  iteration: 35,
                  sequence: 1,
                  thoughts: [],
                  actions: [],
                  window: {
                    totalChars: 4264,
                    stepSpan: 4,
                    segments: { intro: 1177, ledger: 0, steps: 4 },
                    delta: { added: 1, compacted: 0 },
                  },
                },
              ],
            },
          ],
        },
      ],
    }

    render(<ContextTrace ctx={sparseContext} />)

    fireEvent.click(screen.getByRole("button", { name: /phase_build build the project/i }))

    expect(screen.getByText("No branch trace was recorded for this iteration.")).toBeInTheDocument()
  })

  it("paginates very long iteration timelines on demand", () => {
    const manyIterations = Array.from({ length: 95 }, (_, index) => ({
      iteration: index + 1,
      sequence: index + 1,
      thoughts: [`step ${index + 1}`],
      actions: [],
      window: null,
    }))
    const longContext: ContextTraceModel = {
      ...context,
      phases: [
        {
          ...context.phases[0],
          tasks: [{ ...context.phases[0].tasks[0], iterations: manyIterations }],
        },
      ],
    }

    render(<ContextTrace ctx={longContext} />)
    fireEvent.click(screen.getByRole("button", { name: /phase_build build the project/i }))

    // First batch only: iter 40 shown, iter 41 hidden behind "show more".
    expect(screen.getByText("iter 40")).toBeInTheDocument()
    expect(screen.queryByText("iter 41")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Show 40 more · 55 remaining/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /Show 40 more/i }))
    expect(screen.getByText("iter 41")).toBeInTheDocument()
    expect(screen.getByText("iter 80")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Show 15 more · 15 remaining/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /Show 15 more/i }))
    expect(screen.getByText("iter 95")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Show .* more/i })).not.toBeInTheDocument()
  })
})
