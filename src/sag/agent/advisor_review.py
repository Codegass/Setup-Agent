"""Read-only, consultation-local views over already archived evidence.

No paths are opened and no executor tool is dispatched. The source snapshot and
every delivered page are retained by the caller, separately from verdict evidence.
"""

from copy import deepcopy
import hashlib
from io import StringIO
import json

from sag.tools.output_paging import read_text_page

from .advisor_context import _counter, _ranked_extract

EVIDENCE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "advisor_evidence",
            "description": (
                "List, read or search immutable sources from the consultation's catalog. "
                "action=list pages the full catalog using catalog_offset and its returned next cursor. "
                "Use its source_id, never a host/container path. action=read returns a zero-based, "
                "end-exclusive line range. If a long line or page is cut, pass the returned next "
                "start_line and column_offset to continue without gaps. action=search performs a "
                "case-sensitive literal single-line search and returns matching line/column positions "
                "and excerpts; read the surrounding lines to establish context. Search pagination "
                "uses the same next cursor. Empty/missing evidence is unknown, never success. "
                "Only already recorded bytes are available; this tool cannot run commands or read live files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "search"]},
                    "catalog_offset": {"type": "integer", "minimum": 0},
                    "source_id": {
                        "type": "string",
                        "description": "Exact catalog ID for this consultation.",
                    },
                    "query": {
                        "type": "string",
                        "maxLength": 512,
                        "description": "Required for search; literal and case-sensitive, at most 512 characters.",
                    },
                    "start_line": {"type": "integer", "minimum": 0},
                    "end_line": {"type": "integer", "minimum": 0},
                    "column_offset": {"type": "integer", "minimum": 0},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }
]


class AdvisorEvidence:
    def __init__(self, sections):
        self.sources = {}
        for index, section in enumerate(sections, 1):
            self.sources[f"e{index}"] = {
                "name": section.name,
                "original_ref": section.ref,
                "sha256": hashlib.sha256(section.text.encode()).hexdigest(),
                "text": section.text,
            }

    def catalog(self):
        return [
            {
                "source_id": key,
                "name": source["name"],
                "original_ref": source["original_ref"],
                "sha256": source["sha256"],
                "lines": len(source["text"].splitlines()),
                "chars": len(source["text"]),
            }
            for key, source in self.sources.items()
        ]

    def execute(self, name, args, *, max_chars):
        """Validation failures become bounded data; there is no tool registry fallback."""
        try:
            if name != "advisor_evidence" or not isinstance(args, dict):
                raise ValueError("Only advisor_evidence is available")
            if set(args) - {
                "action",
                "source_id",
                "query",
                "start_line",
                "end_line",
                "column_offset",
                "catalog_offset",
            }:
                raise ValueError("Unknown evidence arguments")
            if args.get("action") == "list":
                offset = args.get("catalog_offset", 0)
                if type(offset) is not int or offset < 0:
                    raise ValueError("catalog_offset must be a nonnegative integer")
                catalog, page, used = self.catalog(), [], 0
                for index in range(offset, len(catalog)):
                    size = len(json.dumps(catalog[index], ensure_ascii=False))
                    if page and used + size > max_chars:
                        return {
                            "sources": page,
                            "complete": False,
                            "next": {"catalog_offset": index},
                        }
                    page.append(catalog[index])
                    used += size
                return {"sources": page, "complete": True, "next": None}
            source = self.sources.get(args.get("source_id"))
            if source is None:
                raise ValueError("Unknown source_id; use this consultation's catalog")
            for key in ("start_line", "end_line", "column_offset"):
                if key in args and (type(args[key]) is not int or args[key] < 0):
                    raise ValueError("Line/column offsets must be nonnegative integers")
            start, column = args.get("start_line", 0), args.get("column_offset", 0)
            if args.get("action") == "read":
                page = read_text_page(
                    StringIO(source["text"]),
                    start_line=start,
                    end_line=args.get("end_line"),
                    column_offset=column,
                    max_chars=max_chars,
                )
            elif args.get("action") == "search":
                query = args.get("query")
                if (
                    not isinstance(query, str)
                    or not query
                    or len(query) > 512
                    or "\n" in query
                    or "\r" in query
                ):
                    raise ValueError("Search needs a nonempty literal single-line query")
                page = self._search(source["text"], query, start, column, max_chars)
            else:
                raise ValueError("action must be list, read or search")
            return {
                "source_id": args["source_id"],
                "source_sha256": source["sha256"],
                "original_ref": source["original_ref"],
                **page,
            }
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": str(exc), "status": "unavailable", "complete": False}

    @staticmethod
    def _search(text, query, start, column, max_chars):
        matches, used = [], 0
        for line_number, line in enumerate(text.splitlines(keepends=True)):
            if line_number < start:
                continue
            position = line.find(query, column if line_number == start else 0)
            while position >= 0:
                match = {
                    "line": line_number,
                    "column_offset": position,
                    "excerpt": line[max(0, position - 120) : position + max(200, len(query))][
                        :max_chars
                    ],
                }
                size = len(json.dumps(match, ensure_ascii=False))
                if matches and used + size > max_chars:
                    return {
                        "matches": matches,
                        "complete": False,
                        "next": {"start_line": line_number, "column_offset": position},
                    }
                matches.append(match)
                used += size
                position = line.find(query, position + max(1, len(query)))
        return {"matches": matches, "complete": True, "next": None}


def review_input_size(messages, tools, model):
    """Conservative count including native pairs, JSON framing and tool schemas."""
    count, basis = _counter(model)
    return (
        count(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"messages": messages, "tools": tools}, ensure_ascii=False
                    ),
                }
            ]
        ),
        basis,
    )


def fit_review_messages(initial, exchanges, *, tools, model, input_budget):
    """Keep the task/facts, compact retrieved text only, and retain native pairs.

    Each tool response keeps its source identity and cursor. Originals remain in
    the consultation snapshot. No model is called to summarize these one-use views.
    """
    messages = deepcopy(initial + [message for exchange in exchanges for message in exchange])

    def size():
        return review_input_size(messages, tools, model)[0]

    changes = []
    while size() > input_budget:
        candidates = []
        for index, message in enumerate(messages[len(initial) :], len(initial)):
            if message.get("role") != "tool":
                continue
            value = json.loads(message["content"])
            body = value.get("text")
            if body is None and value.get("matches"):
                body = json.dumps(value["matches"], ensure_ascii=False)
            if body:
                candidates.append((len(body), index, value, body))
        if not candidates:
            if len(messages) <= len(initial):
                raise ValueError(
                    "Advisor window cannot hold the already packed task and tool schema"
                )
            # Drop an entire answered exchange, never orphan tool messages.
            # The full source catalog and protected task/facts survive. This is
            # deterministic compaction, not a skipped consultation.
            start = len(initial)
            end = next(
                (
                    i
                    for i in range(start + 1, len(messages))
                    if messages[i].get("role") == "assistant"
                ),
                len(messages),
            )
            changes.append(
                {
                    "dropped_exchange": [
                        m.get("tool_call_id")
                        for m in messages[start:end]
                        if m.get("role") == "tool"
                    ],
                    "reason": "metadata exceeded capacity; archived sources remain in catalog",
                }
            )
            del messages[start:end]
            continue
        _, index, value, body = max(candidates, key=lambda item: item[0])
        target = len(body) // 2
        value.pop("matches", None)
        value["text"] = _ranked_extract(body, target, set()) if target >= 64 else ""
        value["view_compacted"] = True
        value["omission"] = (
            "Retrieved text was excerpted for capacity; original source and read cursor remain available. Missing text is unknown."
        )
        messages[index]["content"] = json.dumps(value, ensure_ascii=False)
        changes.append(
            {
                "tool_call_id": messages[index]["tool_call_id"],
                "source_id": value.get("source_id"),
                "source_chars": len(body),
                "view_chars": len(value["text"]),
            }
        )
    return messages, {
        "estimated_input_tokens_including_tools": size(),
        "token_count_method": review_input_size(messages, tools, model)[1],
        "retrieval_compaction": changes,
    }
