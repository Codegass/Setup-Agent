import { Terminal } from "lucide-react"

import { Card, CardHead } from "@/components/common/Card"

export function LogsView({ logs }: { logs: string[] }) {
  return (
    <Card className="overflow-hidden">
      <CardHead
        icon={<Terminal size={16} className="text-slate-400" />}
        sub={`${logs.length} lines`}
        title="Raw logs"
      />
      {logs.length ? (
        <pre className="max-h-[520px] overflow-auto bg-[#0d1117] px-4 py-3.5 font-mono text-[12px] leading-relaxed text-slate-200">
          {logs.join("\n")}
        </pre>
      ) : (
        <div className="px-4 py-10 text-center text-[13px] text-slate-400">
          No raw logs captured for this session.
        </div>
      )}
    </Card>
  )
}
