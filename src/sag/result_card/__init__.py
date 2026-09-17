"""One presentation model of a finished run, rendered by three surfaces."""

from sag.result_card.build import build_result_card
from sag.result_card.glosses import REASON_GLOSS, gloss
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import (
    ROW_LABELS,
    ROW_ORDER,
    AttentionItem,
    ResultRow,
    ResultStats,
    RowKey,
    RunResultCard,
    Tone,
)

__all__ = [
    "REASON_GLOSS",
    "ROW_LABELS",
    "ROW_ORDER",
    "AttentionItem",
    "ResultRow",
    "ResultStats",
    "RowKey",
    "RunResultCard",
    "Tone",
    "build_result_card",
    "gloss",
    "render_result_card_markdown",
]
