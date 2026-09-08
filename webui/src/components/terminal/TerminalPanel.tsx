import { FitAddon } from "@xterm/addon-fit"
import { Terminal } from "@xterm/xterm"
import "@xterm/xterm/css/xterm.css"
import { useEffect, useRef, useState } from "react"

type ConnectionStatus = "connecting" | "connected" | "closed" | "error"

interface TerminalPanelProps {
  workspaceId: string
}

export function TerminalPanel({ workspaceId }: TerminalPanelProps) {
  const terminalRef = useRef<HTMLDivElement | null>(null)
  const [status, setStatus] = useState<ConnectionStatus>("connecting")
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const element = terminalRef.current
    if (!element) {
      return
    }

    setStatus("connecting")
    setError(null)

    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
      fontSize: 12,
      lineHeight: 1.35,
      theme: {
        background: "#0d1117",
        foreground: "#d1d5db",
        cursor: "#e5e7eb",
        selectionBackground: "#334155",
      },
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(element)

    const fit = () => {
      try {
        fitAddon.fit()
      } catch {
        // xterm fit can throw while the panel is hidden during tab changes.
      }
    }

    fit()
    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            fit()
          })
    resizeObserver?.observe(element)
    window.addEventListener("resize", fit)

    let disposed = false
    let socket: WebSocket | null = null
    const controller = new AbortController()
    const inputSubscription = term.onData((data) => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(data)
      }
    })

    const connect = async () => {
      try {
        const response = await fetch("/api/terminal-session", {
          headers: { "X-SAG-Client": "workbench" },
          cache: "no-store",
          credentials: "same-origin",
          signal: controller.signal,
        })
        if (!response.ok) throw new Error("Terminal session unavailable")
        const session = await response.json() as { token?: unknown }
        if (disposed) return
        if (typeof session.token !== "string" || !session.token) {
          throw new Error("Terminal session unavailable")
        }
        socket = new WebSocket(buildTerminalWebSocketUrl(workspaceId), [
          "sag-terminal-v1", `sag-session.${session.token}`,
        ])
        socket.binaryType = "arraybuffer"
        socket.onopen = () => {
          if (disposed) return
          setStatus("connected")
          term.write(`Connected to ${workspaceId}\r\n`)
          fit()
        }
        socket.onmessage = (event) => {
          if (!disposed) void writeTerminalMessage(term, event.data)
        }
        socket.onerror = () => {
          if (disposed) return
          setStatus("error")
          setError("Terminal connection failed.")
          term.write("\r\nTerminal connection failed.\r\n")
        }
        socket.onclose = () => {
          if (!disposed) setStatus((current) => (current === "error" ? current : "closed"))
        }
      } catch {
        if (disposed) return
        setStatus("error")
        setError("Terminal connection failed.")
        term.write("\r\nTerminal connection failed.\r\n")
      }
    }
    void connect()

    return () => {
      disposed = true
      controller.abort()
      inputSubscription.dispose()
      resizeObserver?.disconnect()
      window.removeEventListener("resize", fit)
      socket?.close()
      term.dispose()
    }
  }, [workspaceId])

  return (
    <div className="rounded-lg border border-border bg-code-surface">
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-red-400/80" />
            <span className="h-2.5 w-2.5 rounded-full bg-amber-400/80" />
            <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/80" />
          </div>
          <span className="ml-2 truncate font-mono text-[11px] text-code-foreground/60">
            {workspaceId} - interactive shell
          </span>
        </div>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-code-foreground/60">
          {status}
        </span>
      </div>
      <div
        aria-label="Workspace terminal"
        className="h-[420px] min-h-[320px] overflow-hidden px-2 py-2"
        ref={terminalRef}
      />
      {error ? (
        <div className="border-t border-border px-3 py-2 font-mono text-[11px] text-red-300">
          {error}
        </div>
      ) : null}
    </div>
  )
}

function buildTerminalWebSocketUrl(workspaceId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${protocol}//${window.location.host}/api/workspaces/${encodeURIComponent(
    workspaceId,
  )}/terminal`
}

async function writeTerminalMessage(term: Terminal, data: unknown): Promise<void> {
  if (typeof data === "string") {
    term.write(data)
    return
  }
  if (isArrayBufferLike(data)) {
    term.write(new TextDecoder().decode(data))
    return
  }
  if (ArrayBuffer.isView(data)) {
    term.write(new TextDecoder().decode(data))
    return
  }
  if (data instanceof Blob) {
    term.write(new TextDecoder().decode(await data.arrayBuffer()))
  }
}

function isArrayBufferLike(data: unknown): data is ArrayBuffer {
  return Object.prototype.toString.call(data) === "[object ArrayBuffer]"
}
