import { ModuleBreakdownDialog, ModuleTable } from "sag-workbench"
import { modules } from "./_fixtures"

// Rendered open (the Dialog opens on mount); children is the per-module test
// breakdown for the acme-platform reactor — acme-web/acme-cli carry failures.
export const PerModuleBreakdown = () => (
  <ModuleBreakdownDialog onClose={() => {}} title="Per-module test breakdown">
    <ModuleTable modules={modules} variant="test" />
  </ModuleBreakdownDialog>
)

export const SingleModuleDetails = () => (
  <ModuleBreakdownDialog onClose={() => {}} title="Test details">
    <p className="text-[13px] leading-relaxed text-slate-600">
      Surefire reported 1,186 of 1,205 executions passing across 3 modules. 7 failures
      cluster in <span className="font-mono text-slate-800">acme-cli</span> and{" "}
      <span className="font-mono text-slate-800">acme-web</span>; treat the run as partial.
    </p>
  </ModuleBreakdownDialog>
)
