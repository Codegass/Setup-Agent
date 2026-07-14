import { Terminal } from "lucide-react"

import { Card, CardHead } from "@/components/common/Card"

export function LogsView({ logs }: { logs: string[] }) {
  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<Terminal size={16} className="text-muted-foreground" />}
        sub={`${logs.length} lines`}
        title="Raw logs"
      />
      {logs.length ? (
        <div className="max-h-[520px] overflow-auto bg-code-surface py-3 font-mono text-[12px] leading-relaxed">
          {logs.map((line, index) => (
            <div key={index} className="flex">
              <span
                aria-hidden="true"
                className="sticky left-0 w-12 shrink-0 select-none bg-code-surface pr-3 text-right text-code-foreground/50"
              >
                {index + 1}
              </span>
              <span className="whitespace-pre pr-4 text-code-foreground">{line || " "}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="px-4 py-10 text-center text-[13px] text-muted-foreground">
          No raw logs captured for this session.
        </div>
      )}
    </Card>
  )
}
