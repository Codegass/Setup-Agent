"""Per-iteration window manifest (spec §7), stored IN THE CONTAINER.

Context files live inside the docker by design: the agent is superuser
in-container and can introspect/manage its own context — including asking
what compaction removed (the refs it points to are in-container too).
Same heredoc-append pattern as OutputStorageManager. Best-effort: journal
I/O must never break a run."""

import json
import shlex
from typing import Any, Dict

from loguru import logger

JOURNAL_DIR = "/workspace/.setup_agent/contexts/journal"


class ContextJournal:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._dir_ready = False

    def record(self, phase: str, iteration: int, segments: Dict[str, Any],
               delta: Dict[str, Any], total_chars: int) -> None:
        try:
            line = json.dumps({
                "iteration": iteration, "phase": phase,
                "segments": segments, "delta": delta, "total_chars": total_chars,
            })
            prefix = "" if self._dir_ready else f"mkdir -p {JOURNAL_DIR} && "
            path = f"{JOURNAL_DIR}/phase_{phase}.journal.jsonl"
            self.orchestrator.execute_command(
                f"{prefix}printf '%s\\n' {shlex.quote(line)} >> {path}",
                workdir=None, timeout=30,
            )
            self._dir_ready = True
        except Exception as exc:
            logger.debug(f"context journal write skipped: {exc}")
