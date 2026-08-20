import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { LogsView } from "./LogsView"

describe("LogsView", () => {
  afterEach(() => cleanup())

  it("explains historical zero-character tool responses", () => {
    render(
      <LogsView
        logs={[
          "LLM Response from gpt-5.4-mini: 0 chars",
          "🔧 ACTION: search",
        ]}
      />,
    )

    expect(screen.getByText(/counts assistant text only/i)).toBeInTheDocument()
    expect(screen.getByText(/can still contain tool calls/i)).toBeInTheDocument()
  })

  it("does not add the legacy explanation to unrelated logs", () => {
    render(<LogsView logs={["LLM Response from gpt-5.4-mini: tool-only, 1 tool call [search]"]} />)

    expect(screen.queryByText(/counts assistant text only/i)).not.toBeInTheDocument()
  })
})
