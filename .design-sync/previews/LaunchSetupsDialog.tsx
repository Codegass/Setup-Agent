import { LaunchSetupsDialog } from "sag-workbench"

export const Open = () => (
  <LaunchSetupsDialog
    defaultConcurrency={2}
    onClose={() => {}}
    onSubmit={async () => ({ status: 200, batch_id: null, concurrency: 2, accepted: [], rejected: [] })}
    onSubmitted={() => {}}
  />
)
