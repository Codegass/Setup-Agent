import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

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
  static OPEN = 1
  readyState = FakeWebSocket.OPEN

  binaryType: BinaryType = "blob"
  sent: unknown[] = []
  closed = false
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onopen: ((event: Event) => void) | null = null

  constructor(public url: string, public protocols?: string[]) {
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
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeWebSocket)
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ token: "session-capability" }) }))
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
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
    expect(fetch).toHaveBeenCalledWith("/api/terminal-session", expect.objectContaining({
      headers: { "X-SAG-Client": "workbench" }, cache: "no-store",
    }))
    expect(socket.protocols).toEqual(["sag-terminal-v1", "sag-session.session-capability"])
    expect(socket.url).not.toContain("session-capability")

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

  it("shows a bootstrap failure without opening a shell connection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }))
    render(<TerminalPanel workspaceId="sag-demo" />)
    expect(await screen.findByText("Terminal connection failed.")).toBeInTheDocument()
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it("does not connect when a late bootstrap returns after unmount", async () => {
    let resolve: (value: unknown) => void = () => {}
    vi.stubGlobal("fetch", vi.fn(() => new Promise((done) => { resolve = done })))
    const view = render(<TerminalPanel workspaceId="sag-demo" />)
    view.unmount()
    resolve({ ok: true, json: async () => ({ token: "session-capability" }) })
    await Promise.resolve()
    await Promise.resolve()
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

})
