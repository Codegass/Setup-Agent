"""Bounded, checkout-contained document map (Plan 6 Stage A1, spec §C1).

The harness must discover what a repository SAYS about building itself —
README/INSTALL/BUILDING, module docs, CI workflows, Docker and install
scripts, Maven/Gradle/CMake/Python metadata — without putting repository prose
into the model prompt. This module produces that discovery as a map of typed
HANDLES:

    DocumentMapEntry: entry_id, target_sha, path, realpath, source_hash,
                      kind, section_index, parser_version, discovery_status

A handle carries a hash, a kind and line ranges. It never carries file text.
That is the whole untrusted-input defence at this layer: a hostile README can
change what a `section_index` POINTS AT, but it cannot ride the map into a
prompt, a claim or an argv — extraction (lane a2) and execution (Stage B) are
separate, typed stages with their own policy.

Discovery is bounded and contained, and every boundary is a recorded fact:

* enumeration is ONE in-container `find` capped at `MAX_DEPTH`, filtered to
  the candidate kinds, sorted and capped at `MAX_CANDIDATE_PATHS`;
* `realpath` must resolve every candidate UNDER the checkout root, proved by a
  batched in-container probe — an unprovable containment indexes nothing;
* files that could not be indexed are never silently absent: each leaves a
  `partial_map` conflict with a typed reason (`over_budget`, `binary`,
  `symlink_escape`, `generated_tree`, `unreadable`).

`document_map_fingerprint` covers the INDEXED set only, so it answers exactly
one question — did an indexed source change? — and a budget-excluded file can
never move it.

Persistence is best effort (same contract as `invocation_receipts`): this
module never raises. A transport failure degrades to an empty map with a
visible conflict, never to a map that pretends to be complete.
"""

import hashlib
import json
import posixpath
import re
import shlex
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent import invocation_receipts
from sag.agent.evidence_publications import (
    DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
    MutablePublicationObservation,
    evidence_publication_authority_for,
    latest_publication_raw_sha256,
    publish_evidence_revision,
    verify_latest_evidence_bytes,
)
from sag.agent.evidence_records import (
    PublishedJsonObjectRead,
    decode_named_json_record_stream,
    execute_named_json_file_stream,
)
from sag.runtime.container_io import read_container_text
from sag.utils.container_io import (
    WRITE_COMPARE_CONFLICT,
    compare_publish_container_text_atomic,
)

DOCUMENT_MAP_SCHEMA_VERSION = 1
DOCUMENT_MAP_DIR = "/workspace/.setup_agent"
DOCUMENT_MAP_PATH = f"{DOCUMENT_MAP_DIR}/document_map.json"
WORKSPACE_ROOT = "/workspace"

# Shared contract (plan §"Stage A" shared contracts), copied into the run pin.
MAX_FILES = 400
MAX_TOTAL_BYTES = 8_000_000
MAX_FILE_BYTES = 512_000
MAX_DEPTH = 6
PARSER_VERSION = "1"

BUDGETS = {
    "max_files": MAX_FILES,
    "max_total_bytes": MAX_TOTAL_BYTES,
    "max_file_bytes": MAX_FILE_BYTES,
    "max_depth": MAX_DEPTH,
}

# Enumeration bound. `find` is already depth- and name-filtered, but a repo can
# still hold tens of thousands of matching documents; the listing is sorted and
# cut in-container so the transport itself stays bounded. A cut is a conflict,
# never a silent shortening.
MAX_CANDIDATE_PATHS = 4_000
# `realpath` argv bound — containment is proved in batches, not per file.
REALPATH_BATCH_SIZE = 100
# Bytes of the fetched head that decide "binary".
BINARY_SNIFF_BYTES = 8_192
# Sections per entry. A generated 30k-line POM must not produce a 30k-section
# index; the entry says `truncated` instead.
SECTION_INDEX_CAP = 500
# How far a CMake `set(`/`option(` statement may run before the index gives up
# looking for its closing paren.
CMAKE_STATEMENT_MAX_LINES = 40
# Tag-path depth the XML index records (spec §C1: depth ≤ 4).
XML_MAX_DEPTH = 4
# `*.md` is collected under doc/docs and at DOMAIN ROOTS — repo root, module
# root, sub-module root — not repository-wide.
MARKDOWN_ROOT_DEPTH = 3
DOC_DIR_SEGMENTS = ("doc", "docs")
# `*.sh` is collected at the checkout root and in the ci/docker script dirs.
SHELL_DIR_SEGMENTS = ("ci", "docker")

# Trees whose contents are generated or vendored. A vendored 3rdparty README is
# excluded HERE: the surveyed local-provider path has its own machinery for the
# few vendored roots that matter, and indexing them from the map would let a
# vendored document speak for the project.
GENERATED_SEGMENTS = ("build", "target", "dist", "node_modules", "vendor", "3rdparty")

# Every way a candidate can fail to become an entry. A reason outside this set
# is a programming error, not a fact about the checkout.
PARTIAL_REASONS = (
    "over_budget",
    "binary",
    "symlink_escape",
    "generated_tree",
    "unreadable",
)

DISCOVERY_STATUSES = ("indexed", "truncated")

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{7,64}")
_DOCUMENT_KINDS = frozenset(
    {
        "cmake",
        "dockerfile",
        "gradle",
        "markdown",
        "properties",
        "python",
        "requirements",
        "shell",
        "toml",
        "unknown",
        "xml",
        "yaml",
    }
)
_SECTION_KINDS = frozenset(
    {
        "array_table",
        "assignment",
        "code_block",
        "command",
        "directive",
        "heading",
        "job",
        "key",
        "option",
        "set",
        "step",
        "table",
        "tag_path",
    }
)


class DocumentSourceChangedError(RuntimeError):
    """The bytes behind one published map handle changed before extraction."""


# The candidate predicate, in the container's own `find` syntax. Portable
# predicates only (`-name`/`-iname`/`-path`): the depth rules that `-path`
# cannot express — markdown at domain roots, shell at root/ci/docker — are
# applied locally by `is_candidate`, which is the authority on what the map
# collects. `find` may over-return; it must never under-return.
CANDIDATE_PREDICATES = (
    "-iname 'README*'",
    "-iname 'INSTALL*'",
    "-iname 'BUILDING*'",
    "-iname 'CONTRIBUTING*'",
    "-name '*.md'",
    "-path '*/.github/workflows/*.yml'",
    "-path '*/.github/workflows/*.yaml'",
    "-iname 'Dockerfile*'",
    "-name '*.sh'",
    "-name 'pom.xml'",
    "-name 'build.gradle'",
    "-name 'build.gradle.kts'",
    "-name 'settings.gradle'",
    "-name 'settings.gradle.kts'",
    "-name 'gradle.properties'",
    "-name 'CMakeLists.txt'",
    "-name '*.cmake'",
    "-name 'pyproject.toml'",
    "-name 'setup.py'",
    "-name 'requirements*.txt'",
)

DOC_FAMILY_PREFIXES = ("readme", "install", "building", "contributing")
EXACT_CANDIDATE_NAMES = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.properties",
    "cmakelists.txt",
    "pyproject.toml",
    "setup.py",
)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def entry_id(path: str) -> str:
    """`doc-<sha256(path)[:12]>` — the cross-lane entry identity, verbatim."""
    return "doc-" + hashlib.sha256(str(path or "").encode("utf-8")).hexdigest()[:12]


def document_map_fingerprint(entries: Sequence[Any]) -> str:
    """Digest of the sorted `entry_id:source_hash` pairs of the INDEXED set.

    Excluded content is deliberately outside the digest: the fingerprint
    answers "did an indexed source change?", and a budget-excluded file that
    could move it would make every re-survey look like a source change.
    """
    pairs = sorted(
        f"{_field(entry, 'entry_id')}:{_field(entry, 'source_hash')}" for entry in entries or []
    )
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentMapEntry:
    """One indexed document handle (spec §C1 fields verbatim)."""

    entry_id: str
    path: str
    realpath: str
    source_hash: str
    kind: str
    section_index: List[Dict[str, Any]] = field(default_factory=list)
    discovery_status: str = "indexed"
    target_sha: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        """The persisted shape. Absent facts are absent keys, never nulls."""
        body: Dict[str, Any] = {
            "entry_id": self.entry_id,
            "path": self.path,
            "realpath": self.realpath,
            "source_hash": self.source_hash,
            "kind": self.kind,
            "section_index": [dict(section) for section in self.section_index],
            "parser_version": PARSER_VERSION,
            "discovery_status": self.discovery_status,
        }
        target_sha = str(self.target_sha or "").strip()
        if target_sha:
            body["target_sha"] = target_sha
        return body


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def discover_document_map(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    checkout_root: str,
    *,
    target_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Enumerate, contain, bound and index the documents under `checkout_root`.

    Three probe shapes, in this order: one `find`, one batched `realpath`, one
    bounded `head -c` per file that survives to indexing. The order is the
    budget law — containment is proved before any content is fetched, and the
    file cap stops the read loop rather than filtering its results.

    Never raises. A failure anywhere degrades to fewer entries plus a typed
    `partial_map` conflict; it never degrades to a map that claims completeness.
    """
    root = posixpath.normpath(str(checkout_root or "").strip())
    conflicts: Dict[str, str] = {}
    if not _inside_workspace(root):
        logger.debug(f"document map skipped: {checkout_root!r} is outside {WORKSPACE_ROOT}")
        return _result([], conflicts)

    listing, exhausted = _enumerate(execute, root)
    if listing is None:
        return _result([], {root: "unreadable"})
    if exhausted:
        conflicts[root] = "over_budget"

    candidates = sorted({relative for relative in listing if is_candidate(relative)})
    kept = []
    for relative in candidates:
        if is_generated(relative):
            conflicts[posixpath.join(root, relative)] = "generated_tree"
        else:
            kept.append(relative)

    # The file budget stops the pipeline; it does not filter its output. Every
    # path past the cap is a recorded exclusion, and none of them is fetched.
    selected, over_budget = kept[:MAX_FILES], kept[MAX_FILES:]
    for relative in over_budget:
        conflicts[posixpath.join(root, relative)] = "over_budget"

    paths = [posixpath.join(root, relative) for relative in selected]
    resolved = _resolve_paths(execute, root, paths)
    if resolved is None:
        # Containment could not be PROVED. Unknown containment is not benign
        # containment, so nothing is indexed.
        return _result([], {root: "unreadable"})

    contained = []
    for path in paths:
        realpath = resolved.get(path)
        if realpath is None:
            conflicts[path] = "symlink_escape"
        else:
            contained.append((path, realpath))

    sha = str(target_sha or "").strip() or None
    if sha is None and contained:
        sha = _probe_target_sha(execute, root)
    entries: List[DocumentMapEntry] = []
    total_bytes = 0
    for position, (path, realpath) in enumerate(contained):
        text = _read_head(execute, path)
        if text is None:
            conflicts[path] = "unreadable"
            continue
        raw = _raw_bytes(text)
        if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
            conflicts[path] = "binary"
            continue
        if total_bytes + len(raw) > MAX_TOTAL_BYTES:
            for remaining, _ in contained[position:]:
                conflicts[remaining] = "over_budget"
            break
        total_bytes += len(raw)
        entries.append(_build_entry(path, realpath, text, raw, sha))

    return _result(entries, conflicts)


def _result(entries: Sequence[DocumentMapEntry], conflicts: Mapping[str, str]) -> Dict[str, Any]:
    ordered = sorted(entries, key=lambda entry: entry.path)
    return {
        "entries": ordered,
        "document_map_fingerprint": document_map_fingerprint(ordered),
        "partial_map": [{"path": path, "reason": conflicts[path]} for path in sorted(conflicts)],
    }


def _build_entry(
    path: str,
    realpath: str,
    text: str,
    raw: bytes,
    target_sha: Optional[str],
) -> DocumentMapEntry:
    kind = detect_kind(path, text)
    section_index, index_truncated = _build_section_index(kind, text)
    truncated = index_truncated or len(raw) >= MAX_FILE_BYTES
    return DocumentMapEntry(
        entry_id=entry_id(path),
        path=path,
        realpath=realpath,
        source_hash=hashlib.sha256(raw).hexdigest(),
        kind=kind,
        section_index=section_index,
        discovery_status="truncated" if truncated else "indexed",
        target_sha=target_sha,
    )


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def enumeration_command(root: str) -> str:
    """The one bounded `find` the map is allowed to run."""
    predicates = " -o ".join(CANDIDATE_PREDICATES)
    return (
        f"cd {shlex.quote(root)} && "
        f"find . -maxdepth {MAX_DEPTH} \\( -type f -o -type l \\) "
        f"\\( {predicates} \\) -print 2>/dev/null "
        f"| LC_ALL=C sort | head -n {MAX_CANDIDATE_PATHS + 1}"
    )


def _enumerate(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    root: str,
) -> Tuple[Optional[List[str]], bool]:
    """Candidate paths relative to `root`, and whether the listing was cut."""
    try:
        result = execute(enumeration_command(root)) or {}
    except Exception as exc:  # discovery never breaks the caller
        logger.debug(f"document map enumeration failed: {exc}")
        return None, False
    if not _succeeded(result):
        return None, False
    relatives = []
    for line in str(result.get("output") or "").splitlines():
        candidate = line.strip()
        if candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate and candidate not in (".", ".."):
            relatives.append(candidate)
    exhausted = len(relatives) > MAX_CANDIDATE_PATHS
    return relatives[:MAX_CANDIDATE_PATHS], exhausted


def _resolve_paths(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    root: str,
    paths: Sequence[str],
) -> Optional[Dict[str, str]]:
    """Real paths of `paths` that stay under `root`; None when unprovable.

    Batched: one `realpath` per `REALPATH_BATCH_SIZE` candidates, each batch
    re-resolving the root so a batch is self-contained. A batch whose reply
    does not line up with its request proves nothing, so the whole probe fails
    rather than half-trusting the lines that happened to arrive.
    """
    if not paths:
        return {}
    contained: Dict[str, str] = {}
    for start in range(0, len(paths), REALPATH_BATCH_SIZE):
        batch = list(paths[start : start + REALPATH_BATCH_SIZE])
        arguments = " ".join(shlex.quote(path) for path in [root, *batch])
        try:
            result = execute(f"realpath -m -- {arguments}") or {}
        except Exception as exc:
            logger.debug(f"document map containment probe failed: {exc}")
            return None
        if not _succeeded(result):
            return None
        lines = [line.strip() for line in str(result.get("output") or "").splitlines()]
        lines = [line for line in lines if line]
        if len(lines) != len(batch) + 1:
            return None
        root_real = lines[0].rstrip("/")
        if not root_real or root_real == "/":
            return None
        for path, realpath in zip(batch, lines[1:]):
            if realpath == root_real or realpath.startswith(f"{root_real}/"):
                contained[path] = realpath
    return contained


def _read_head(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    path: str,
) -> Optional[str]:
    """The first `MAX_FILE_BYTES` of `path`, or None when it is unreadable."""
    try:
        result = execute(f"head -c {MAX_FILE_BYTES} -- {shlex.quote(path)} 2>/dev/null") or {}
    except Exception as exc:
        logger.debug(f"document {path} unreadable: {exc}")
        return None
    if not _succeeded(result):
        return None
    return str(result.get("output") or "")


def read_entry_text(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    entry: Any,
) -> Optional[str]:
    """The indexed text of one map entry, under the SAME budget discovery used.

    The map deliberately carries handles rather than text, so every consumer
    that needs the bytes back — claim extraction, targeted retrieval — fetches
    them again. This is the one fetch they share, so the `MAX_FILE_BYTES` bound
    is stated once: a consumer with its own `cat` would read past the bytes the
    entry was hashed and indexed over, and then be talking about a different
    document than the one the handle names.

    When the handle carries ``source_hash``, the exact bounded bytes fetched
    here must still match it.  A mismatch raises ``DocumentSourceChangedError``
    so a claim extractor can record a typed survey conflict without treating
    the changed document as the bytes the published handle named.

    None when the entry names no path or the read failed; an unreadable
    document states nothing, and nothing is guessed on its behalf.
    """
    path = _field(entry, "path")
    if not path:
        return None
    text = _read_head(execute, path)
    if text is None:
        return None
    expected = _field(entry, "source_hash").strip().lower()
    if expected:
        actual = hashlib.sha256(_raw_bytes(text)).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
            raise DocumentSourceChangedError(f"document bytes changed after mapping: {path}")
    return text


def _probe_target_sha(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    root: str,
) -> Optional[str]:
    """The checkout sha, via the same probe the receipts use. None when absent."""
    try:
        return invocation_receipts.target_sha(execute, root)
    except Exception as exc:
        logger.debug(f"document map target sha unavailable: {exc}")
        return None


# ---------------------------------------------------------------------------
# candidate rules
# ---------------------------------------------------------------------------


def is_candidate(relative_path: str) -> bool:
    """Whether a discovered path is one of the candidate document kinds.

    This is the authority, not the `find` predicate: `find` cannot express
    "markdown at a domain root" or "shell at the root or under ci/docker", so
    it over-returns and this narrows.
    """
    relative = str(relative_path or "").strip().strip("/")
    if not relative:
        return False
    parts = relative.split("/")
    name = parts[-1]
    lowered = name.lower()
    directories = [part.lower() for part in parts[:-1]]
    _, _, extension = lowered.rpartition(".")
    extension = f".{extension}" if "." in lowered else ""

    if lowered.startswith(DOC_FAMILY_PREFIXES):
        return True
    if lowered.startswith("dockerfile"):
        return True
    if lowered in EXACT_CANDIDATE_NAMES or extension == ".cmake":
        return True
    if lowered.startswith("requirements") and extension == ".txt":
        return True
    if extension in (".yml", ".yaml"):
        return ".github/workflows/" in relative
    if extension == ".md":
        return (
            any(directory in DOC_DIR_SEGMENTS for directory in directories)
            or len(parts) <= MARKDOWN_ROOT_DEPTH
        )
    if extension == ".sh":
        return len(parts) == 1 or any(directory in SHELL_DIR_SEGMENTS for directory in directories)
    return False


def is_generated(relative_path: str) -> bool:
    """Whether the path lives inside a generated or vendored tree."""
    parts = str(relative_path or "").strip().strip("/").split("/")
    return any(directory in GENERATED_SEGMENTS for directory in parts[:-1])


# ---------------------------------------------------------------------------
# kind detection
# ---------------------------------------------------------------------------


def detect_kind(path: str, text: str = "") -> str:
    """The typed kind of a document, by extension first and content second.

    The extension wins wherever it is decisive — a `README.md` full of shell is
    still markdown. Content only resolves what the name leaves open, which is
    exactly the extensionless doc-family case (`BUILDING` may be a script).
    """
    name = posixpath.basename(str(path or "")).lower()
    _, _, extension = name.rpartition(".")
    extension = f".{extension}" if "." in name else ""

    if name in ("cmakelists.txt",) or extension == ".cmake":
        return "cmake"
    if name.startswith("dockerfile"):
        return "dockerfile"
    if extension in (".md", ".markdown"):
        return "markdown"
    if extension in (".yml", ".yaml"):
        return "yaml"
    if extension == ".xml":
        return "xml"
    if extension == ".toml":
        return "toml"
    if extension == ".sh":
        return "shell"
    if extension in (".gradle", ".kts") or name.startswith(("build.gradle", "settings.gradle")):
        return "gradle"
    if extension == ".properties":
        return "properties"
    if extension == ".py":
        return "python"
    if name.startswith("requirements") and extension == ".txt":
        return "requirements"
    if name.startswith(DOC_FAMILY_PREFIXES):
        content_kind = _kind_from_content(text)
        return content_kind or "markdown"
    return _kind_from_content(text) or "unknown"


def _kind_from_content(text: str) -> Optional[str]:
    head = str(text or "").lstrip()[:200]
    if head.startswith("#!") and re.match(r"#![^\n]*\b(ba|da|k|z)?sh\b", head):
        return "shell"
    if head.startswith("<?xml"):
        return "xml"
    return None


# ---------------------------------------------------------------------------
# section indexing
# ---------------------------------------------------------------------------


def build_section_index(kind: str, text: str) -> List[Dict[str, Any]]:
    """The bounded `section_index` of one document (spec §C1 shape, verbatim).

    A kind with no typed extractor at `parser_version` 1 indexes to nothing:
    the entry still exists and is still hashed, and its structure is EXPLICITLY
    unknown rather than guessed.
    """
    return _build_section_index(kind, text)[0]


def _build_section_index(kind: str, text: str) -> Tuple[List[Dict[str, Any]], bool]:
    indexer = _INDEXERS.get(str(kind or ""))
    if indexer is None:
        return [], False
    lines = str(text or "").splitlines()
    try:
        raw = indexer(str(text or ""), lines)
    except Exception as exc:  # a hostile document cannot break discovery
        logger.debug(f"section index for kind {kind!r} abandoned: {exc}")
        return [], False
    return _finalize(raw, len(lines))


def _finalize(
    raw: Sequence[Tuple[str, str, int, int]],
    line_count: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Order, bound and identify the sections of one document."""
    cleaned = []
    for section_kind, title, start, end in raw:
        start = max(1, int(start))
        end = min(max(start, int(end)), max(line_count, start))
        cleaned.append((start, end, str(section_kind), str(title)))
    cleaned.sort()
    truncated = len(cleaned) > SECTION_INDEX_CAP
    return (
        [
            {
                "section_id": f"sec-{ordinal:04d}",
                "kind": section_kind,
                "title_or_key": title,
                "start_line": start,
                "end_line": end,
            }
            for ordinal, (start, end, section_kind, title) in enumerate(cleaned[:SECTION_INDEX_CAP])
        ],
        truncated,
    )


_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _index_markdown(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Headings and fenced blocks — a heading alone is not an extractor.

    The fence's info string is kept verbatim (`bash`, `console`, ``): lane a2
    needs it to decide whether a block is a lifecycle command, and this layer
    must not decide that for it.
    """
    sections: List[Tuple[str, str, int, int]] = []
    headings: List[int] = []
    fence: Optional[Tuple[str, str, int]] = None
    for number, line in enumerate(lines, start=1):
        fence_match = _MD_FENCE.match(line)
        if fence is not None:
            marker, info, start = fence
            if (
                fence_match
                and fence_match.group(1)[0] == marker[0]
                and len(fence_match.group(1)) >= len(marker)
            ):
                sections.append(("code_block", info, start, number))
                fence = None
            continue
        if fence_match:
            fence = (fence_match.group(1), fence_match.group(2).strip(), number)
            continue
        heading = _MD_HEADING.match(line)
        if heading and heading.group(2).strip():
            sections.append(("heading", heading.group(2).strip(), number, len(lines)))
            headings.append(len(sections) - 1)
    if fence is not None:
        marker, info, start = fence
        sections.append(("code_block", info, start, len(lines)))
    # A heading section runs to the line before the next heading of any level.
    for position, index in enumerate(headings[:-1]):
        kind, title, start, _ = sections[index]
        sections[index] = (kind, title, start, sections[headings[position + 1]][2] - 1)
    return sections


_YAML_TOP_KEY = re.compile(r"^([A-Za-z_][\w.\-]*|\"[^\"]*\"|'[^']*'):(\s|$)")
_YAML_KEY = re.compile(r"^(\s*)([A-Za-z_][\w.\-]*|\"[^\"]*\"|'[^']*'):(\s|$)")
_YAML_ITEM = re.compile(r"^(\s*)-(\s|$)")


def _index_yaml(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Top-level keys, `jobs.<name>` and `jobs.<name>.steps[i]` paths.

    An indentation walk, not a YAML load: the index is a map of LINE RANGES, and
    a parsed document has thrown those away. Bounded by construction — it only
    ever descends the three levels the workflow shape needs.
    """
    sections: List[Tuple[str, str, int, int]] = []
    tops: List[Tuple[str, int]] = []
    for number, line in enumerate(lines, start=1):
        if _is_blank_or_comment(line):
            continue
        if _YAML_TOP_KEY.match(line):
            tops.append((_unquote(line.split(":", 1)[0]), number))
    for position, (name, start) in enumerate(tops):
        end = tops[position + 1][1] - 1 if position + 1 < len(tops) else len(lines)
        sections.append(("key", name, start, end))
        if name == "jobs":
            sections.extend(_index_yaml_jobs(lines, start + 1, end))
    return sections


def _index_yaml_jobs(
    lines: Sequence[str],
    start: int,
    end: int,
) -> List[Tuple[str, str, int, int]]:
    sections: List[Tuple[str, str, int, int]] = []
    jobs = _blocks_at_first_indent(lines, start, end, _YAML_KEY)
    for name, job_start, job_end in jobs:
        sections.append(("job", f"jobs.{name}", job_start, job_end))
        steps = _blocks_at_first_indent(lines, job_start + 1, job_end, _YAML_KEY)
        for step_name, step_start, step_end in steps:
            if step_name != "steps":
                continue
            items = _blocks_at_first_indent(lines, step_start + 1, step_end, _YAML_ITEM)
            for ordinal, (_, item_start, item_end) in enumerate(items):
                sections.append(("step", f"jobs.{name}.steps[{ordinal}]", item_start, item_end))
    return sections


def _blocks_at_first_indent(
    lines: Sequence[str],
    start: int,
    end: int,
    pattern: "re.Pattern[str]",
) -> List[Tuple[str, int, int]]:
    """Sibling blocks in `[start, end]` that share the first matched indent."""
    found: List[Tuple[str, int]] = []
    indent: Optional[int] = None
    for number in range(start, end + 1):
        if number > len(lines):
            break
        line = lines[number - 1]
        if _is_blank_or_comment(line):
            continue
        match = pattern.match(line)
        if not match:
            continue
        depth = len(match.group(1))
        if indent is None:
            indent = depth
        if depth != indent:
            continue
        name = _unquote(line.strip().split(":", 1)[0]) if pattern is _YAML_KEY else ""
        found.append((name, number))
    blocks = []
    for position, (name, block_start) in enumerate(found):
        block_end = found[position + 1][1] - 1 if position + 1 < len(found) else end
        blocks.append((name, block_start, block_end))
    return blocks


_XML_MASKS = (r"<!--.*?-->", r"<!\[CDATA\[.*?\]\]>", r"<\?.*?\?>", r"<!DOCTYPE[^>]*>")
_XML_TAG = re.compile(r"<(/?)([A-Za-z_][\w.\-]*(?::[\w.\-]+)?)([^>]*?)(/?)>", re.S)


def _index_xml(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Tag paths bounded to `XML_MAX_DEPTH`, with their open/close line span."""
    masked = _mask(text, _XML_MASKS)
    line_of = _line_lookup(masked)
    sections: List[Tuple[str, str, int, int]] = []
    stack: List[Tuple[str, int]] = []
    for match in _XML_TAG.finditer(masked):
        closing, name, _, self_closing = match.groups()
        number = line_of(match.start())
        if closing:
            while stack:
                open_name, open_line = stack.pop()
                if open_name != name:
                    continue
                if len(stack) + 1 <= XML_MAX_DEPTH:
                    path = "/".join([ancestor for ancestor, _ in stack] + [name])
                    sections.append(("tag_path", path, open_line, number))
                break
            continue
        if self_closing:
            if len(stack) + 1 <= XML_MAX_DEPTH:
                path = "/".join([ancestor for ancestor, _ in stack] + [name])
                sections.append(("tag_path", path, number, number))
            continue
        stack.append((name, number))
    return sections


_TOML_TABLE = re.compile(r"^\s*(\[\[?)\s*([^\]\s][^\]]*?)\s*(\]\]?)\s*$")


def _index_toml(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Tables and arrays-of-tables, each running to the next header."""
    headers = []
    for number, line in enumerate(lines, start=1):
        match = _TOML_TABLE.match(line)
        if match and len(match.group(1)) == len(match.group(3)):
            kind = "array_table" if match.group(1) == "[[" else "table"
            headers.append((kind, match.group(2).strip(), number))
    sections = []
    for position, (kind, name, start) in enumerate(headers):
        end = headers[position + 1][2] - 1 if position + 1 < len(headers) else len(lines)
        sections.append((kind, name, start, end))
    return sections


_CMAKE_CALL = re.compile(r"^\s*(set|option)\s*\(\s*([^\s()]*)", re.IGNORECASE)


def _index_cmake(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """`set()` / `option()` statements, spanning to their closing paren."""
    sections = []
    number = 1
    while number <= len(lines):
        match = _CMAKE_CALL.match(lines[number - 1])
        if not match:
            number += 1
            continue
        depth = 0
        end = number
        for offset in range(min(CMAKE_STATEMENT_MAX_LINES, len(lines) - number + 1)):
            line = lines[number - 1 + offset]
            depth += line.count("(") - line.count(")")
            end = number + offset
            if depth <= 0:
                break
        sections.append((match.group(1).lower(), match.group(2), number, end))
        number = end + 1
    return sections


_SH_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
_PROPERTY_ASSIGN = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*[=:]")


def _index_shell(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Variable assignments and command lines, continuations folded in."""
    sections = []
    number = 1
    while number <= len(lines):
        line = lines[number - 1]
        if _is_blank_or_comment(line, comment="#"):
            number += 1
            continue
        end = _continuation_end(lines, number)
        assignment = _SH_ASSIGN.match(line)
        if assignment:
            sections.append(("assignment", assignment.group(1), number, end))
        else:
            words = line.strip().split()
            if words:
                sections.append(("command", words[0], number, end))
        number = end + 1
    return sections


def _index_properties(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """`key=value` lines — dotted keys included, which shell syntax forbids."""
    sections = []
    for number, line in enumerate(lines, start=1):
        if _is_blank_or_comment(line, comment="#!"):
            continue
        match = _PROPERTY_ASSIGN.match(line)
        if match:
            sections.append(("assignment", match.group(1), number, number))
    return sections


DOCKER_DIRECTIVES = (
    "FROM",
    "RUN",
    "CMD",
    "LABEL",
    "MAINTAINER",
    "EXPOSE",
    "ENV",
    "ADD",
    "COPY",
    "ENTRYPOINT",
    "VOLUME",
    "USER",
    "WORKDIR",
    "ARG",
    "ONBUILD",
    "STOPSIGNAL",
    "HEALTHCHECK",
    "SHELL",
)

_DOCKER_DIRECTIVE = re.compile(r"^\s*([A-Za-z]+)(\s|$)")


def _index_dockerfile(text: str, lines: Sequence[str]) -> List[Tuple[str, str, int, int]]:
    """Directives with their continuation span — `RUN` is a dependency source."""
    sections = []
    number = 1
    while number <= len(lines):
        line = lines[number - 1]
        if _is_blank_or_comment(line, comment="#"):
            number += 1
            continue
        match = _DOCKER_DIRECTIVE.match(line)
        end = _continuation_end(lines, number)
        if match and match.group(1).upper() in DOCKER_DIRECTIVES:
            sections.append(("directive", match.group(1).upper(), number, end))
        number = end + 1
    return sections


_INDEXERS: Dict[str, Callable[[str, Sequence[str]], List[Tuple[str, str, int, int]]]] = {
    "markdown": _index_markdown,
    "yaml": _index_yaml,
    "xml": _index_xml,
    "toml": _index_toml,
    "cmake": _index_cmake,
    "shell": _index_shell,
    "properties": _index_properties,
    "dockerfile": _index_dockerfile,
}


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def validate_document_map_v1(
    payload: Mapping[str, Any],
    expected_id: str = DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
) -> Dict[str, Any]:
    """Validate one complete canonical DocumentMap v1 object.

    Publication authority answers *who authorized these exact bytes*; this
    validator separately answers *whether those bytes are the documented map
    schema*.  The stable host publication identity is not carried in the
    repository-writable mirror, so ``expected_id`` is fixed at this boundary.
    """

    if expected_id != DOCUMENT_MAP_LOGICAL_ARTIFACT_ID:
        raise ValueError("document map publication identity is invalid")
    if not isinstance(payload, Mapping):
        raise ValueError("document map must be an object")
    body = dict(payload)
    _require_exact_keys(
        body,
        required={
            "schema_version",
            "parser_version",
            "document_map_fingerprint",
            "entries",
            "partial_map",
        },
        field="document map",
    )
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("document map schema_version is not v1")
    if body["parser_version"] != PARSER_VERSION:
        raise ValueError("document map parser_version is not current")

    entries = body["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError("document map entries are not a bounded list")
    entry_paths: List[str] = []
    entry_ids: set[str] = set()
    target_shas: List[str] = []
    target_presence: List[bool] = []
    for position, candidate in enumerate(entries):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"document map entry {position} is not an object")
        record = dict(candidate)
        _require_exact_keys(
            record,
            required={
                "entry_id",
                "path",
                "realpath",
                "source_hash",
                "kind",
                "section_index",
                "parser_version",
                "discovery_status",
            },
            optional={"target_sha"},
            field=f"document map entry {position}",
        )
        path = _canonical_workspace_path(record["path"], f"entry {position} path")
        _canonical_workspace_path(record["realpath"], f"entry {position} realpath")
        identity = record["entry_id"]
        if type(identity) is not str or identity != entry_id(path) or identity in entry_ids:
            raise ValueError(f"document map entry {position} identity is not canonical")
        entry_ids.add(identity)
        entry_paths.append(path)
        if (
            type(record["source_hash"]) is not str
            or _SHA256_RE.fullmatch(record["source_hash"]) is None
        ):
            raise ValueError(f"document map entry {position} source_hash is invalid")
        if type(record["kind"]) is not str or record["kind"] not in _DOCUMENT_KINDS:
            raise ValueError(f"document map entry {position} kind is invalid")
        if record["parser_version"] != PARSER_VERSION:
            raise ValueError(f"document map entry {position} parser_version is invalid")
        if record["discovery_status"] not in DISCOVERY_STATUSES:
            raise ValueError(f"document map entry {position} discovery_status is invalid")
        _validate_section_index(record["section_index"], position)

        has_target = "target_sha" in record
        target_presence.append(has_target)
        if has_target:
            target = record["target_sha"]
            if type(target) is not str or _GIT_OBJECT_RE.fullmatch(target) is None:
                raise ValueError(f"document map entry {position} target_sha is invalid")
            target_shas.append(target)

    if entry_paths != sorted(entry_paths) or len(set(entry_paths)) != len(entry_paths):
        raise ValueError("document map entries are not in canonical path order")
    if target_presence and any(target_presence) and not all(target_presence):
        raise ValueError("document map target_sha coverage is incomplete")
    if len(set(target_shas)) > 1:
        raise ValueError("document map entries disagree on target_sha")

    fingerprint = body["document_map_fingerprint"]
    if (
        type(fingerprint) is not str
        or _SHA256_RE.fullmatch(fingerprint) is None
        or fingerprint != document_map_fingerprint(entries)
    ):
        raise ValueError("document map fingerprint is invalid")

    partial_map = body["partial_map"]
    if not isinstance(partial_map, list) or len(partial_map) > MAX_CANDIDATE_PATHS + 1:
        raise ValueError("document map partial_map is not a bounded list")
    partial_paths: List[str] = []
    for position, candidate in enumerate(partial_map):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"document map partial entry {position} is not an object")
        conflict = dict(candidate)
        _require_exact_keys(
            conflict,
            required={"path", "reason"},
            field=f"document map partial entry {position}",
        )
        path = _canonical_workspace_path(conflict["path"], f"partial entry {position} path")
        reason = conflict["reason"]
        if type(reason) is not str or reason not in PARTIAL_REASONS:
            raise ValueError(f"document map partial entry {position} reason is invalid")
        partial_paths.append(path)
    if partial_paths != sorted(partial_paths) or len(set(partial_paths)) != len(partial_paths):
        raise ValueError("document map partial entries are not in canonical path order")
    if set(entry_paths).intersection(partial_paths):
        raise ValueError("document map cannot both index and exclude one path")
    if len(entries) + len(partial_map) > MAX_CANDIDATE_PATHS + 1:
        raise ValueError("document map exceeds the discovery boundary")
    return body


def read_live_document_map(source: Any) -> PublishedJsonObjectRead:
    """Read the fixed map mirror only at its exact current host revision.

    The single framed read preserves the physical ``document_map.json`` name
    and exact bytes.  One authority lock then verifies the stable logical head,
    latest revision, tombstone state and the complete current document-map set.
    A verified absent head returns ``complete=True, payload=None``; callers that
    require Stage A to have produced a map decide whether absence is acceptable.
    """

    try:
        result = execute_named_json_file_stream(source, DOCUMENT_MAP_PATH)
    except Exception as exc:
        return PublishedJsonObjectRead(conflict="stream_unreadable", detail=str(exc))
    decoded = decode_named_json_record_stream(result)
    if not decoded.complete or decoded.conflict is not None or len(decoded.records) > 1:
        return PublishedJsonObjectRead(conflict=decoded.conflict or "stream_unreadable")

    authority = evidence_publication_authority_for(source)
    payload: Dict[str, Any] | None = None
    raw: bytes | None = None
    observed: Dict[str, MutablePublicationObservation] = {}
    if decoded.records:
        record = decoded.records[0]
        if record.filename != posixpath.basename(DOCUMENT_MAP_PATH):
            return PublishedJsonObjectRead(
                conflict="record_filename_invalid",
                detail=record.filename,
            )
        try:
            normalized = validate_document_map_v1(
                record.payload,
                DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
            )
            if normalized != record.payload:
                raise ValueError("document map is not canonical")
        except (KeyError, TypeError, ValueError) as exc:
            return PublishedJsonObjectRead(
                conflict="record_schema_invalid",
                detail=f"{record.filename}: {exc}",
            )
        observed[DOCUMENT_MAP_LOGICAL_ARTIFACT_ID] = MutablePublicationObservation(
            logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
            raw_sha256=record.raw_sha256,
            byte_count=record.byte_count,
            run_id=str(getattr(authority, "run_id", "") or ""),
        )
        payload = normalized
        raw = record.raw

    check = authority.verify_latest_record_set("document_map", observed)
    if not check.authorized:
        return PublishedJsonObjectRead(
            conflict=f"publication_set_{check.status}",
            detail=check.detail or check.status,
        )
    return PublishedJsonObjectRead(payload=payload, raw=raw, complete=True)


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> None:
    allowed = required | (optional or set())
    if not required.issubset(value) or set(value) - allowed:
        raise ValueError(f"{field} has missing or unknown fields")


def _canonical_workspace_path(value: Any, field: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError(f"{field} is invalid")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} is invalid")
    if posixpath.normpath(value) != value or not _inside_workspace(value):
        raise ValueError(f"{field} is not a canonical workspace path")
    return value


def _validate_section_index(value: Any, entry_position: int) -> None:
    if not isinstance(value, list) or len(value) > SECTION_INDEX_CAP:
        raise ValueError(f"document map entry {entry_position} section_index is invalid")
    order: List[Tuple[int, int, str, str]] = []
    for position, candidate in enumerate(value):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"document map entry {entry_position} section {position} is invalid")
        section = dict(candidate)
        _require_exact_keys(
            section,
            required={"section_id", "kind", "title_or_key", "start_line", "end_line"},
            field=f"document map entry {entry_position} section {position}",
        )
        if section["section_id"] != f"sec-{position:04d}":
            raise ValueError(f"document map entry {entry_position} section id is not canonical")
        if type(section["kind"]) is not str or section["kind"] not in _SECTION_KINDS:
            raise ValueError(f"document map entry {entry_position} section kind is invalid")
        title = section["title_or_key"]
        if (
            type(title) is not str
            or len(title) > MAX_FILE_BYTES
            or "\x00" in title
            or "\n" in title
            or "\r" in title
        ):
            raise ValueError(f"document map entry {entry_position} section title is invalid")
        start = section["start_line"]
        end = section["end_line"]
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end > MAX_FILE_BYTES + 1
        ):
            raise ValueError(f"document map entry {entry_position} section range is invalid")
        order.append((start, end, section["kind"], title))
    if order != sorted(order):
        raise ValueError(f"document map entry {entry_position} sections are not canonical")


def document_map_payload(document_map: Mapping[str, Any]) -> Dict[str, Any]:
    """The persisted body: the map, its fingerprint and its conflict list."""
    entries = [_entry_payload(entry) for entry in (document_map or {}).get("entries") or []]
    fingerprint = str((document_map or {}).get("document_map_fingerprint") or "")
    return {
        "schema_version": DOCUMENT_MAP_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "document_map_fingerprint": fingerprint or document_map_fingerprint(entries),
        "entries": entries,
        "partial_map": [
            dict(conflict) for conflict in (document_map or {}).get("partial_map") or []
        ],
    }


def write_document_map(
    execute: Callable[..., Optional[Mapping[str, Any]]],
    document_map: Mapping[str, Any],
) -> bool:
    """Persist the map atomically (temp file + `mv`). False on failure.

    Same mechanism as `invocation_receipts.write_receipt`: no reader ever sees
    half a map, and a failed write is a returned fact rather than an exception.
    """
    try:
        body = json.dumps(document_map_payload(document_map), sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"document map is not serializable: {exc}")
        return False
    raw = body.encode("utf-8")
    for _attempt in range(3):
        try:
            authority = evidence_publication_authority_for(execute)
            head = authority.latest_head(DOCUMENT_MAP_LOGICAL_ARTIFACT_ID)
            expected_publication = latest_publication_raw_sha256(
                execute, DOCUMENT_MAP_LOGICAL_ARTIFACT_ID
            )
        except Exception as exc:
            logger.debug(f"document map host authority unavailable: {exc}")
            return False
        try:
            content = read_container_text(
                _ExecuteOnly(execute), DOCUMENT_MAP_PATH, exact_bytes=True
            )
        except Exception as exc:
            logger.debug(f"document map base is unreadable: {exc}")
            return False
        if head is not None:
            if head.publication_state == "revoked":
                return False
            current = bool(
                content is not None
                and verify_latest_evidence_bytes(
                    execute,
                    record_kind="document_map",
                    record_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                    logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                    raw=content.encode("utf-8"),
                ).authorized
            )
            if not current:
                # Complete the host half of an interrupted exact same write;
                # the desired map was independently recomputed above.
                if content == body:
                    return publish_evidence_revision(
                        execute,
                        record_kind="document_map",
                        record_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                        logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                        raw=raw,
                        expected_previous_raw_sha256=expected_publication,
                    ).published
                return False
        try:
            result = compare_publish_container_text_atomic(
                execute,
                DOCUMENT_MAP_PATH,
                body,
                expected_content=content,
                validate_json=True,
            )
        except Exception as exc:
            logger.debug(f"document map not persisted: {exc}")
            return False
        if result.persisted:
            publication = publish_evidence_revision(
                execute,
                record_kind="document_map",
                record_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                logical_artifact_id=DOCUMENT_MAP_LOGICAL_ARTIFACT_ID,
                raw=raw,
                expected_previous_raw_sha256=expected_publication,
            )
            if publication.published:
                return True
            logger.warning(
                "Document map reached the container but host publication failed: "
                f"{publication.status}"
            )
            return False
        if result.code != WRITE_COMPARE_CONFLICT:
            logger.debug(f"document map not persisted: {result.code}")
            return False
    return False


class _ExecuteOnly:
    """Lossless-reader adapter for writers that receive an execute callable."""

    def __init__(self, execute: Callable[..., Optional[Mapping[str, Any]]]) -> None:
        self._execute = execute
        # Explicit in-memory test-double transport used by read_container_text.
        owner = getattr(execute, "__self__", None)
        for candidate in (execute, owner):
            persisted = getattr(candidate, "persisted", None)
            if isinstance(persisted, dict):
                self.files = persisted
                break
            files = getattr(candidate, "files", None)
            if isinstance(files, dict):
                self.files = files
                break

    def execute_command(self, command: str, **kwargs: Any) -> Optional[Mapping[str, Any]]:
        return self._execute(command, **kwargs)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entry_payload(entry: Any) -> Dict[str, Any]:
    payload = getattr(entry, "payload", None)
    return payload() if callable(payload) else dict(entry)


def _field(entry: Any, name: str) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get(name) or "")
    return str(getattr(entry, name, "") or "")


def _inside_workspace(path: str) -> bool:
    return path.startswith(f"{WORKSPACE_ROOT}/") and path != WORKSPACE_ROOT


def _raw_bytes(text: str) -> bytes:
    """The bytes of the fetched head, as fetched."""
    try:
        return text.encode("utf-8", "surrogateescape")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text.encode("utf-8", "replace")


def _is_blank_or_comment(line: str, comment: str = "#") -> bool:
    stripped = line.strip()
    return not stripped or stripped[0] in comment


def _continuation_end(lines: Sequence[str], number: int) -> int:
    end = number
    while end < len(lines) and lines[end - 1].rstrip().endswith("\\"):
        end += 1
    return end


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _mask(text: str, patterns: Sequence[str]) -> str:
    """Blank out regions while keeping every newline, so lines still line up."""
    masked = str(text or "")
    for pattern in patterns:
        masked = re.sub(
            pattern,
            lambda match: re.sub(r"[^\n]", " ", match.group(0)),
            masked,
            flags=re.S | re.IGNORECASE,
        )
    return masked


def _line_lookup(text: str) -> Callable[[int], int]:
    """Offset -> 1-indexed line, without rescanning the document per tag."""
    starts = [0]
    for position, character in enumerate(text):
        if character == "\n":
            starts.append(position + 1)
    return lambda offset: bisect_right(starts, offset)


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)
