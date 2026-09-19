"""The reader's setup report, rendered on the host from a finished run.

The report a reader opens is produced here, at run end, from the run's own
record — not inside the agent loop by a tool the model calls. A tool called
during the run is called before the run is over: the one it replaced took its
counts twelve ledger events short of the end and stated 18 turns for a run of
19. Nothing in this package can make that mistake, because it takes a session
directory and never an engine.
"""

from sag.report_document.render import render_setup_report, report_name

__all__ = ["render_setup_report", "report_name"]
