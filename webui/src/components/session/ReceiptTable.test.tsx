import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ReceiptSummary } from "@/api/types"

import { ReceiptTable } from "./ReceiptTable"

afterEach(() => cleanup())

// Every fixture below is a row `GET /api/sessions/{id}` served for a real run
// under `logs/`, copied field for field. The fully-populated shape the plan
// wrote its one fixture from is 224 of 1,195 archived receipts; the other 971
// are missing at least one of the four optional fields, and the three shapes
// below are what that actually looks like.

/** `logs/advisor-high20-terra-high-gson-20260914` — everything stated. */
const full: ReceiptSummary = {
  receiptId: "inv-maven-1-f006510e44c8-0001",
  tool: "maven",
  argv: "/opt/apache-maven-3.9.9/bin/mvn clean verify",
  workingDirectory: "/workspace/gson",
  actualCwd: "/workspace/gson",
  exitCode: 0,
  outcome: "completed",
  lifecycleState: "finished",
  toolchain: {
    executable: "/opt/apache-maven-3.9.9/bin/mvn",
    version: "Apache Maven 3.9.9 (8e8579a9e76f7d015ee5ec7bfcdc97d260186937)",
  },
  jdkMajor: "17",
  jdkVersion: "17.0.20",
  reportsNew: 138,
  reportsChanged: 0,
  testsReported: 4866,
}

/** `logs/d3r2-23-v4-20260909/runs/kafka` — the wrapper states no version, and
 *  the JDK is a major with no runtime reading. 843 of 1,195 receipts are
 *  major-only; 312 state no tool version. */
const majorOnly: ReceiptSummary = {
  receiptId: "inv-gradle-1-02d23cf468ca-0002",
  tool: "gradle",
  argv: "/workspace/kafka/gradlew --continue unitTest",
  workingDirectory: "/workspace/kafka",
  actualCwd: "/workspace/kafka",
  exitCode: 1,
  outcome: "failed",
  lifecycleState: "finished",
  toolchain: { executable: "/workspace/kafka/gradlew", version: null },
  jdkMajor: "17",
  jdkVersion: null,
  reportsNew: 1626,
  reportsChanged: 0,
  testsReported: null,
}

/** `logs/recovery-v4-freemarker-mini-3-20260912` — a native binary run through
 *  bash: no toolchain record at all, and no Java. */
const bare: ReceiptSummary = {
  receiptId: "inv-bash-native-463aa590bbf7-0005",
  tool: "bash",
  argv: "./freemarker-test-graalvm-native/build/native/nativeCompile/freemarker-test-graalvm-native",
  workingDirectory: "/workspace/freemarker",
  actualCwd: "/workspace/freemarker",
  exitCode: 0,
  outcome: "completed",
  lifecycleState: "finished",
  toolchain: null,
  jdkMajor: null,
  jdkVersion: null,
  reportsNew: 0,
  reportsChanged: 0,
  testsReported: null,
}

/** `logs/verification-ten-extension-v6-20260910` — rewrote reports it did not
 *  create, which the plan's `{reportsNew} new` rule alone would print as `0`. */
const rewrote: ReceiptSummary = {
  ...majorOnly,
  receiptId: "inv-maven-1-85c181c6de88-0002",
  tool: "maven",
  argv: "/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -pl httpclient5-testing -am test",
  toolchain: {
    executable: "/opt/apache-maven-3.9.16/bin/mvn",
    version: "Apache Maven 3.9.16 (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)",
  },
  reportsNew: 0,
  reportsChanged: 337,
}

/** One receipt's row, so a cell assertion cannot pass on another row's text. */
function row(receiptId: string): HTMLElement {
  const node = document.querySelector(`[data-receipt="${receiptId}"]`)
  expect(node, `no row for ${receiptId}`).not.toBeNull()
  return node as HTMLElement
}

function cell(receiptId: string, column: string): string {
  return (
    row(receiptId).querySelector(`[data-column="${column}"]`)?.textContent ?? "(no such column)"
  )
}

describe("ReceiptTable", () => {
  it("shows the command that actually ran, with the directory it ran in", () => {
    render(<ReceiptTable receipts={[full]} />)
    expect(cell(full.receiptId, "command")).toContain("/opt/apache-maven-3.9.9/bin/mvn clean verify")
    expect(cell(full.receiptId, "command")).toContain("/workspace/gson")
  })

  it("shows the toolchain the run observed, not the one it asked for", () => {
    render(<ReceiptTable receipts={[full]} />)
    expect(cell(full.receiptId, "tool")).toContain("maven")
    expect(cell(full.receiptId, "tool")).toContain("Apache Maven 3.9.9")
    expect(cell(full.receiptId, "java")).toBe("JDK 17 · 17.0.20")
  })

  it("states a Java major with no runtime reading as the major alone", () => {
    render(<ReceiptTable receipts={[majorOnly]} />)
    // Not "JDK 17 · " with a dangling separator, and not "—", which would throw
    // away a major the payload does state. 843 of 1,195 receipts are this shape.
    expect(cell(majorOnly.receiptId, "java")).toBe("JDK 17")
  })

  it("names a tool that recorded no version by its name alone", () => {
    render(<ReceiptTable receipts={[majorOnly, bare]} />)
    expect(cell(majorOnly.receiptId, "tool")).toBe("gradle")
    expect(cell(bare.receiptId, "tool")).toBe("bash")
    expect(cell(bare.receiptId, "java")).toBe("—")
  })

  it("counts the report files the command wrote, and the ones it rewrote", () => {
    render(<ReceiptTable receipts={[full, rewrote, bare]} />)
    expect(cell(full.receiptId, "reports")).toBe("138 new")
    expect(cell(rewrote.receiptId, "reports")).toBe("337 changed")
    expect(cell(bare.receiptId, "reports")).toBe("—")
  })

  it("marks a non-zero exit code", () => {
    render(<ReceiptTable receipts={[full, majorOnly]} />)
    expect(cell(full.receiptId, "exit")).toBe("0")
    expect(cell(majorOnly.receiptId, "exit")).toBe("1")
    expect(row(majorOnly.receiptId).querySelector('[data-column="exit"]')).toHaveClass(
      "text-status-failed",
    )
  })

  it("states absence rather than zero for an unknown exit code", () => {
    render(<ReceiptTable receipts={[{ ...full, exitCode: null, testsReported: null }]} />)
    expect(cell(full.receiptId, "exit")).toBe("—")
    expect(cell(full.receiptId, "tests")).toBe("—")
  })

  it("counts the tests a command reported", () => {
    render(<ReceiptTable receipts={[full, majorOnly]} />)
    expect(cell(full.receiptId, "tests")).toBe("4,866")
    expect(cell(majorOnly.receiptId, "tests")).toBe("—")
  })

  it("teaches when there is nothing to show", () => {
    render(<ReceiptTable receipts={[]} />)
    expect(screen.getByText("No commands were recorded for this run.")).toBeInTheDocument()
  })

  it("says in its own words what this list is, wherever it is placed", () => {
    render(
      <ReceiptTable
        caption="Commands that wrote test reports."
        empty="No command in this run wrote a test report."
        receipts={[]}
      />,
    )
    expect(screen.getByText("No command in this run wrote a test report.")).toBeInTheDocument()
    expect(screen.queryByText("No commands were recorded for this run.")).not.toBeInTheDocument()
  })

  it("names the table for a screen reader by the caption it was given", () => {
    render(<ReceiptTable caption="Every command this run dispatched." receipts={[full]} />)
    const table = screen.getByRole("table", { name: "Every command this run dispatched." })
    expect(within(table).getByText("Command")).toBeInTheDocument()
  })
})
