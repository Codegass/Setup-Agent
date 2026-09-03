"""The v2 external-target metric layer.

A target record is what the project's own CI proved on the same revision: per
cell, which tests actually ran, which stayed red after every retry, and whether
the cell's green conclusion was laundered by ``continue-on-error``.  It is the
outside yardstick a run's own result is measured against.

The success certificate stays in :mod:`sag.agent.java_success_certificates`.
That module judges one SAG run against its own sealed obligations; this package
judges a run against an external target.  The dependency runs one way only: the
adapter in :mod:`sag.metrics.attainment` reads a finished certificate, and the
certificate module never imports this package.

Every module here is pure: no filesystem, process, or network authority.  Stored
metrics carry integer counts only; a fraction is an integer numerator/denominator
pair so a digest of the record never depends on float formatting.
"""
