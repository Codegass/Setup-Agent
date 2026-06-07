import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { LaunchQueueState } from "@/api/types"

import { LaunchQueuePanel } from "./LaunchQueuePanel"

const queue: LaunchQueueState = {
  default_concurrency: 4,
  summary: { queued: 2, launching: 1, running: 1, completed: 7, failed: 1 },
  batches: [
    {
      id: "BATCH-20260607-abcdef",
      status: "running",
      concurrency: 3,
      created: "2026-06-07T02:30:00",
      items: [
        {
          id: "LAUNCH-12345678",
          row_index: 0,
          repo_url: "https://github.com/apache/commons-cli.git",
          workspace_id: "sag-commons-cli-111",
          ref: "rel/commons-cli-1.11.0",
          status: "running",
          pid: 12345,
          exit_code: null,
          error: null,
          process_log:
            "logs/project_launches/BATCH-20260607-abcdef/LAUNCH-12345678.log",
        },
      ],
    },
    {
      id: "BATCH-20260606-ffffff",
      status: "failed",
      concurrency: 2,
      created: "2026-06-06T01:00:00",
      items: [
        {
          id: "LAUNCH-87654321",
          row_index: 0,
          repo_url: "https://github.com/x/broken.git",
          workspace_id: "sag-broken",
          ref: null,
          status: "failed",
          pid: 222,
          exit_code: 1,
          error: "sag project exited with code 1",
          process_log:
            "logs/project_launches/BATCH-20260606-ffffff/LAUNCH-87654321.log",
        },
      ],
    },
  ],
}

describe("LaunchQueuePanel", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders compact status counts", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("3 queued")).toBeInTheDocument()
    expect(screen.getByText("1 running")).toBeInTheDocument()
    expect(screen.getByText("7 completed")).toBeInTheDocument()
    expect(screen.getByText("1 failed")).toBeInTheDocument()
  })

  it("shows the active batch with its items", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("BATCH-20260607-abcdef")).toBeInTheDocument()
    expect(screen.getByText("sag-commons-cli-111")).toBeInTheDocument()
  })

  it("lists recent failed launches with error and process log provenance", () => {
    render(<LaunchQueuePanel queue={queue} />)

    expect(screen.getByText("sag-broken")).toBeInTheDocument()
    expect(screen.getByText(/exited with code 1/)).toBeInTheDocument()
    expect(
      screen.getByText(
        "logs/project_launches/BATCH-20260606-ffffff/LAUNCH-87654321.log",
      ),
    ).toBeInTheDocument()
  })
})
