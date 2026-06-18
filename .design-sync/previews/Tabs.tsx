import { Tabs } from "sag-workbench"

export const FacetNav = () => (
  <Tabs
    value="build"
    onChange={() => {}}
    tabs={[
      { id: "build", label: "Build" },
      { id: "test", label: "Test", count: 320 },
      { id: "flow", label: "Flow" },
      { id: "evidence", label: "Evidence", count: 1 },
      { id: "logs", label: "Logs", disabled: true },
    ]}
  />
)
