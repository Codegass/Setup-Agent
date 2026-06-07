import { cleanup, render, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { TerminalPanel } from "./TerminalPanel"

const terminalState = vi.hoisted(() => ({
  dataHandler: null as ((data: string) => void) | null,
  fitCalls: 0,
  instances: [] as Array<{ writes: unknown[]; disposed: boolean }>,
}))

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    writes: unknown[] = []
    disposed = false

    constructor() {
      terminalState.instances.push(this)
    }

    loadAddon() {}

    open() {}

    write(data: unknown) {
      this.writes.push(data)
    }

    onData(handler: (data: string) => void) {
      terminalState.dataHandler = handler
      return { dispose: vi.fn() }
    }

    dispose() {
      this.disposed = true
    }
  },
}))

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {
      terminalState.fitCalls += 1
    }
  },
}))

class FakeWebSocket {
  static instances: FakeWebSocket[] = []

  binaryType: BinaryType = "blob"
  sent: unknown[] = []
  closed = false
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onopen: ((event: Event) => void) | null = null

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }

  send(data: unknown) {
    this.sent.push(data)
  }

  close() {
    this.closed = true
  }
}

describe("TerminalPanel", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    FakeWebSocket.instances = []
    terminalState.dataHandler = null
    terminalState.fitCalls = 0
    terminalState.instances = []
  })

  it("connects to the workspace terminal websocket and bridges xterm data", async () => {
    vi.stubGlobal("WebSocket", FakeWebSocket)
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    )

    render(<TerminalPanel workspaceId="sag-commons-cli" />)

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const socket = FakeWebSocket.instances[0]
    expect(socket.url).toMatch(/\/api\/workspaces\/sag-commons-cli\/terminal$/)
    expect(socket.binaryType).toBe("arraybuffer")

    socket.onopen?.(new Event("open"))
    socket.onmessage?.(
      new MessageEvent("message", {
        data: new TextEncoder().encode("ready\r\n").buffer,
      }),
    )
    terminalState.dataHandler?.("pwd\n")

    await waitFor(() => expect(terminalState.instances[0].writes).toContain("ready\r\n"))
    expect(socket.sent).toContain("pwd\n")
    expect(terminalState.fitCalls).toBeGreaterThan(0)
  })
})
