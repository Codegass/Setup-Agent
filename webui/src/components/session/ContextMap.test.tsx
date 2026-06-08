import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ContextMap as ContextMapModel } from "@/api/types"

import { ContextMap } from "./ContextMap"

const context: ContextMapModel = {
  trunk: {
    goal: "Set up commons-lang",
    state: "completed",
    progress: { done: 4, total: 5 },
    summary: "",
  },
  tasks: [
    {
      id: "task_4",
      title: "Run tests using Maven",
      status: "completed",
      evidenceStatus: "conflict",
      summary:
        "Previous task (task_3): Maven compile passed; build_status=success; project_path=/workspace/commons-lang\nmaven succeeded: BUILD SUCCESS\nFull output ref: output_full_test_log",
      refs: [
        {
          ref: "output_full_test_log",
          label: "output_full_test_log",
          content: "Full Maven output\nBUILD SUCCESS\nTail line",
          contentLength: 41,
          tool: "maven",
        },
      ],
      evidenceRefs: [
        {
          ref: "output_validator_conflict",
          label: "validator conflict",
          content: "Validator saw one failing test after Maven reported success.",
          contentLength: 55,
          tool: "test-validator",
        },
      ],
      conflicts: ["Maven summary says success but validator found one failed test."],
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
}

describe("ContextMap", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders a single Done/Total progress bar", () => {
    render(<ContextMap ctx={context} />)

    expect(screen.getByText("Done / Total")).toBeInTheDocument()
    expect(screen.getByText("4 / 5")).toBeInTheDocument()
    expect(screen.getAllByRole("progressbar")).toHaveLength(1)
    expect(screen.queryByText(/^done$/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/^total$/i)).not.toBeInTheDocument()
  })

  it("formats expanded branch details without hiding the full summary", () => {
    render(<ContextMap ctx={context} />)

    fireEvent.click(screen.getByRole("button", { name: /task_4/i }))

    expect(screen.getByText("Previous task")).toBeInTheDocument()
    expect(screen.getByText("(task_3): Maven compile passed")).toBeInTheDocument()
    expect(screen.getByText("build_status")).toBeInTheDocument()
    expect(screen.getByText("success")).toBeInTheDocument()
    expect(screen.getByText("project_path")).toBeInTheDocument()
    expect(screen.getByText("/workspace/commons-lang")).toBeInTheDocument()
    expect(screen.getByText("maven succeeded")).toBeInTheDocument()
    expect(screen.getByText("BUILD SUCCESS")).toBeInTheDocument()
    expect(screen.getAllByText("output_full_test_log")).toHaveLength(2)
  })

  it("opens a full output preview from a branch ref", () => {
    render(<ContextMap ctx={context} />)

    fireEvent.click(screen.getByRole("button", { name: /task_4/i }))
    const refs = screen.getAllByRole("button", { name: "output_full_test_log" })
    fireEvent.click(refs[1])

    expect(screen.getByText("Output preview")).toBeInTheDocument()
    expect(screen.getByText(/Full Maven output/)).toBeInTheDocument()
    expect(screen.getByText(/Tail line/)).toBeInTheDocument()
  })

  it("opens a full output preview from an inline output ref in branch details", () => {
    render(<ContextMap ctx={context} />)

    fireEvent.click(screen.getByRole("button", { name: /task_4/i }))
    const refs = screen.getAllByRole("button", { name: "output_full_test_log" })
    expect(refs).toHaveLength(2)
    const inlineRef = refs[0]

    fireEvent.click(inlineRef)

    expect(screen.getByText("Output preview")).toBeInTheDocument()
    expect(screen.getByText(/Full Maven output/)).toBeInTheDocument()
  })

  it("shows task evidence status, conflicts, and evidence reference previews", () => {
    render(<ContextMap ctx={context} />)

    expect(screen.getByText("Conflict")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /task_4/i }))

    expect(screen.getByText("Conflicts")).toBeInTheDocument()
    expect(
      screen.getByText("Maven summary says success but validator found one failed test."),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "validator conflict" }))

    expect(screen.getByText("Output preview")).toBeInTheDocument()
    expect(screen.getByText(/Validator saw one failing test/)).toBeInTheDocument()
  })

  it("does not duplicate evidence refs already folded into task refs", () => {
    render(
      <ContextMap
        ctx={{
          ...context,
          tasks: [
            {
              ...context.tasks[0],
              summary: "",
              refs: [
                ...context.tasks[0].refs,
                {
                  ref: "output_validator_conflict",
                  label: "validator conflict",
                  content: "Validator saw one failing test after Maven reported success.",
                  contentLength: 55,
                  tool: "test-validator",
                },
              ],
            },
          ],
        }}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /task_4/i }))

    expect(screen.getAllByRole("button", { name: "validator conflict" })).toHaveLength(1)
    expect(screen.queryByText("Evidence refs")).not.toBeInTheDocument()
  })
})
