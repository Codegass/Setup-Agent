import { ModuleTable } from "sag-workbench"
import { Card } from "sag-workbench"
import { modules } from "./_fixtures"

export const BuildVariant = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <ModuleTable modules={modules} variant="build" />
  </Card>
)

export const TestVariant = () => (
  <Card style={{ overflow: "hidden", width: 720 }}>
    <ModuleTable modules={modules} variant="test" />
  </Card>
)
