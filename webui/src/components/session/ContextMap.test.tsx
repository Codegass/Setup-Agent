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
      summary:
        "Previous task (task_3): Maven compile passed; build_status=success; project_path=/workspace/commons-lang\nmaven succeeded: BUILD SUCCESS\nFull output ref: output_full_test_log",
      refs: ["output_full_test_log"],
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
})
