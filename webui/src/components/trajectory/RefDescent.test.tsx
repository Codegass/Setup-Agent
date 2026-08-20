import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { RefDescent } from "./RefDescent"

describe("RefDescent", () => {
  afterEach(cleanup)

  it("renders a stored message as readable content with real newlines", () => {
    const content = "=== PHASE: ANALYZE ===\nRun picture\n✓ provision"
    const bytes = JSON.stringify({ content, role: "user" })

    render(
      <RefDescent
        outputs={{ output_message: bytes }}
        refName="output_message"
        status="ready"
      />,
    )

    expect(screen.getByText(/3 lines/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "output_message" }))

    expect(screen.getByText("Formatted content")).toBeInTheDocument()
    expect(screen.getByText("role")).toBeInTheDocument()
    expect(screen.getByText("user")).toBeInTheDocument()
    expect(screen.getByTestId("ref-output-output_message").textContent).toBe(content)
    expect(screen.getByTestId("ref-output-output_message").textContent).not.toContain("\\n")

    fireEvent.click(screen.getByRole("button", { name: /show raw json/i }))
    expect(screen.getByText("Raw JSON")).toBeInTheDocument()
    expect(screen.getByTestId("ref-output-output_message").textContent).toBe(bytes)

    fireEvent.click(screen.getByRole("button", { name: /show formatted output/i }))
    expect(screen.getByTestId("ref-output-output_message").textContent).toBe(content)
  })

  it("pretty-prints JSON without a primary text field", () => {
    const value = { items: [1, 2], nested: { ok: true } }

    render(
      <RefDescent
        outputs={{ output_json: JSON.stringify(value) }}
        refName="output_json"
        status="ready"
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "output_json" }))

    expect(screen.getByText("Formatted JSON")).toBeInTheDocument()
    expect(screen.getByTestId("ref-output-output_json").textContent).toBe(
      JSON.stringify(value, null, 2),
    )
  })

  it("leaves non-JSON output unchanged", () => {
    const bytes = "line one\nline two"

    render(
      <RefDescent
        outputs={{ output_text: bytes }}
        refName="output_text"
        status="ready"
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "output_text" }))

    expect(screen.getByText("Plain text")).toBeInTheDocument()
    expect(screen.getByTestId("ref-output-output_text").textContent).toBe(bytes)
    expect(screen.queryByRole("button", { name: /show raw json/i })).not.toBeInTheDocument()
  })

  it("explains that a job ref is a handle rather than missing output bytes", () => {
    render(<RefDescent outputs={{}} refName="job:58db946542a8" status="ready" />)
    fireEvent.click(screen.getByRole("button", { name: "job:58db946542a8" }))

    expect(screen.getByText(/background-job handle/i)).toBeInTheDocument()
  })
})
