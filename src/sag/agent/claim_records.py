"""Typed claim records and deterministic policy extractors (Plan 6 Stage A2).

Spec §C1: the document map and the claims drawn from it are DIFFERENT objects.
A `DocumentMapEntry` says "these bytes exist at this path with this hash"; a
`ClaimRecord` says what those bytes STATE. Keeping them apart is what lets a
claim carry its own provenance class, so the loop can tell a README sentence
from a probe result without re-reading either — and so contradicting a probe
never silently contradicts the prose that motivated it.

`ClaimRecord` is a discriminated union on `source_class`, and each variant
carries only its own typed `source_ref`:

    repository_doc / config -> PolicyClaim    (entry_id + source_hash + range)
    physical                -> PhysicalClaim  (probe id + content hash + scope)
    receipt                 -> ReceiptClaim   (receipt + assessment + predicate)
    inferred                -> InferredClaim  (rule id + complete support set)

A receipt id in a document field is a validation error rather than a coerced
value (spec §6 "Claim union"), which is why every source ref forbids extra
keys and nothing is stringified on the way in.

Two asymmetries are deliberate:

* documentation alone can never state a CAPABILITY. `kind="capability"` is
  refused on `repository_doc`/`config`, because "the README says LLVM is
  supported" is a claim about the README, not about this machine.
* an `UntrustedDocInterpretation` — a model's reading of an opaque prose
  excerpt — is not a claim and has no conversion path into one. There is no
  constructor, method or annotation in this module that accepts one. Turning
  prose into policy requires a deterministic adapter, which is what the
  extractors below are.

The extractors are pure `(entry, text) -> [PolicyClaim]` functions. They
record LITERALS: a version range that is written down, an argv that is written
down, a pin that is written down. Prose without a version literal, and
headings without commands, extract nothing — an extractor that guesses would
put the model's anchoring problem back into the survey.

Argv is preserved verbatim and a cwd is only ever taken from an explicit
context statement (`working-directory:`, a fenced `cd`, the checkout root).
The Bigtop anchor turns on this: `mvn ... -f ./bigtop-test-framework/pom.xml`
runs from the repository root, and rewriting that `-f` into a cwd would be a
normalization nobody documented.

Persistence mirrors `evidence_assessments`: one atomic file per claim at
`/workspace/.setup_agent/claims/<claim_id>.json`, idempotent for the same
body and refused (never merged) for a different body under the same id. This
module never raises; a failed write is a fact the caller reports, not an
exception the runner has to survive.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    Callable,
    Iterable,
    Literal,
    Mapping,
    NamedTuple,
    Sequence,
    Union,
)

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from sag.agent.control_events import canonical_sha256

CLAIM_SCHEMA_VERSION = 1
CLAIM_DIR = "/workspace/.setup_agent/claims"
# Heredoc delimiter for the atomic write. The body is single-line JSON, so no
# claim content can ever collide with it.
CLAIM_HEREDOC = "SAGCLAIM"
CLAIM_ID_DIGEST_CHARS = 12
DETAIL_MAX_CHARS = 200

# Spec §C1/§C5: the three independent dimensions of a claim. Source class says
# WHERE it came from, source status whether that source is still current, and
# evidence status what execution has shown about it. A claim can be
# `current`+`untested` forever without that being a failure.
SOURCE_CLASSES = ("repository_doc", "config", "physical", "receipt", "inferred")
DOCUMENTATION_SOURCE_CLASSES = ("repository_doc", "config")
SOURCE_STATUSES = ("current", "stale", "superseded", "conflicted")
EVIDENCE_STATUSES = (
    "untested",
    "unknown",
    "confirmed",
    "blocked",
    "contradicted",
    "not_applicable",
)
CLAIM_KINDS = ("tool_constraint", "lifecycle", "dependency", "env", "capability")

SourceClass = Literal["repository_doc", "config", "physical", "receipt", "inferred"]
SourceStatus = Literal["current", "stale", "superseded", "conflicted"]
EvidenceStatus = Literal[
    "untested", "unknown", "confirmed", "blocked", "contradicted", "not_applicable"
]
ClaimKind = Literal["tool_constraint", "lifecycle", "dependency", "env", "capability"]

# The first token a documented command must resolve to before it is recorded as
# a lifecycle claim. `./gradlew` resolves through its basename; `make`, `git`
# and `echo` resolve to nothing and are simply not lifecycle facts.
LIFECYCLE_RUNNERS = {
    "mvn": "maven",
    "mvnw": "maven",
    "gradle": "gradle",
    "gradlew": "gradle",
    "pip": "pip",
    "pip3": "pip",
    "pytest": "pytest",
    "cmake": "cmake",
    "python": "python",
    "python3": "python",
}

MARKDOWN_KINDS = ("markdown", "md", "rst", "text")
# Every document-map kind some extractor below can read. It is stated here, in
# the module that owns the extractors, because the survey has to decide whether
# fetching an entry's text can produce anything at all — a `pyproject.toml` is
# worth INDEXING and is worth no second read, and a caller that re-derived this
# list would eventually disagree with the extractors it is describing.
EXTRACTOR_ENTRY_KINDS = MARKDOWN_KINDS + (
    "yaml",
    "yml",
    "dockerfile",
    "docker",
    "shell",
    "sh",
    "bash",
    "cmake",
    "xml",
    "pom",
    "requirements",
)
SHELL_FENCE_LANGUAGES = (
    "",
    "sh",
    "bash",
    "zsh",
    "shell",
    "console",
    "shell-session",
    "sh-session",
    "bash-session",
    "cmd",
    "commandline",
)
# Where the shell operators end one command and start the next. A package list
# must not run past `&&` into the next program's arguments.
SHELL_OPERATORS = ("&&", "||", ";", "|", "&", ">", ">>", "<")

_MAVEN_REQUIRE_VERSION = re.compile(
    r"<require(?P<tool>Maven|Java)Version>\s*<version>\s*(?P<constraint>[^<\s][^<]*?)\s*</version>",
    re.IGNORECASE,
)
# A prose constraint is recorded only when a tool name is followed by a VERSION
# LITERAL. "You need Maven and a JDK" states a dependency, not a constraint,
# and inventing one would be the guess this survey exists to avoid.
_PROSE_TOOL_VERSION = re.compile(
    r"\b(?P<tool>Maven|Gradle|CMake|JDK|Java|Python|Node(?:\.js)?)\b"
    r"(?:\s+version)?\s*"
    r"(?P<operator>>=|<=|==|~=|>|<)?\s*"
    r"(?P<version>\d+(?:\.\d+)*(?:\.\*|\+)?)\b",
    re.IGNORECASE,
)
_PROSE_TOOL_NAMES = {
    "maven": "maven",
    "gradle": "gradle",
    "cmake": "cmake",
    "jdk": "java",
    "java": "java",
    "python": "python",
    "node": "node",
    "node.js": "node",
}
_PIP_REQUIREMENT = re.compile(
    r"^(?P<package>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?)"
    r"(?P<specifier>==|>=|<=|~=|!=|<|>)"
    r"(?P<version>[^\s,;]+)$"
)
_APT_PACKAGE = re.compile(
    r"^(?P<package>[A-Za-z0-9][A-Za-z0-9.+_-]*)" r"(?:(?P<specifier>=)(?P<version>\S+))?$"
)
# `name=value` but NEVER `name==constraint`: a pip pin is not an assignment
# (live tvm r3: `numpy==1.26.*` minted an env claim with value "=1.26.*").
_SHELL_ASSIGNMENT = re.compile(
    r"^(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>(?!=).*)$"
)
_CMAKE_DEFINITION = re.compile(r"-D(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s\"']+)")
_CMAKE_SET = re.compile(
    r"^\s*set\s*\(\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<value>[^\s()\"']+)[^)]*\)",
    re.IGNORECASE,
)
_CMAKE_OPTION = re.compile(
    r"^\s*option\s*\(\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+" r"\"[^\"]*\"\s+(?P<value>ON|OFF)\s*\)",
    re.IGNORECASE,
)
_YAML_KEY = re.compile(r"^\s*(?:-\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):\s*(?P<value>.*)$")
_YAML_BLOCK_SCALAR = ("|", ">", "|-", ">-", "|+", ">+")
_REQUIREMENTS_FILE = re.compile(r"^(?:.*[-_])?(?:requirements|constraints)(?:[-_][\w.]+)?\.txt$")
_DOCKERFILE_FROM = re.compile(r"^FROM\s+\S+(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.IGNORECASE)
_DOCKERFILE_RUN = re.compile(r"^RUN\s+(?P<command>.*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# claim identity
# ---------------------------------------------------------------------------


def claim_id(kind: str, source_ref: BaseModel | Mapping[str, Any]) -> str:
    """`<kind>-<sha256(canonical source_ref)[:12]>` (plan Stage A contract).

    Identity is the SOURCE, not the value: two extractors reading the same
    range of the same bytes state the same claim, and a source whose bytes
    changed states a new one (the hash is part of the ref).
    """
    reference = (
        source_ref.model_dump(mode="json")
        if isinstance(source_ref, BaseModel)
        else dict(source_ref)
    )
    digest = canonical_sha256(reference)[:CLAIM_ID_DIGEST_CHARS]
    return f"{str(kind).strip()}-{digest}"


# ---------------------------------------------------------------------------
# the typed source refs — one per source class, no shared keys
# ---------------------------------------------------------------------------


class PolicySourceRef(BaseModel):
    """Where a document/config claim was read: entry, bytes, range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    # "L12", "L12-L18", or "L12#1" for the nth claim of one kind on one line.
    source_range: str = Field(min_length=1)


class PhysicalSourceRef(BaseModel):
    """What was observed, from which snapshot, over how much of the tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    observed_scope: str = Field(min_length=1)


class ReceiptSourceRef(BaseModel):
    """Which receipt, which assessment of it, which predicate it satisfied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str = Field(min_length=1)
    assessment_id: str = Field(min_length=1)
    predicate_id: str = Field(min_length=1)


class InferredSourceRef(BaseModel):
    """Which deterministic rule fired, over which COMPLETE support set.

    The support set is required: an inference whose support is unrecorded
    cannot be retracted when one of its supports is contradicted, which is the
    whole point of the causal loop (spec §C5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1)
    support_claim_ids: tuple[str, ...] = Field(min_length=1)


class Applicability(BaseModel):
    """When a claim applies. Every field is absent unless the entry states it.

    `os`, `arch` and `goal` are never inferred by an extractor — a workflow
    that happens to say `runs-on: ubuntu-22.04` states where CI runs, not
    where this claim applies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str | None = None
    os: str | None = None
    arch: str | None = None
    workflow_job: str | None = None
    dockerfile_stage: str | None = None
    goal: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if value is not None
        }


class UntrustedDocInterpretation(BaseModel):
    """A model's reading of an opaque prose excerpt. NOT a claim.

    It carries the text and the entry range it came from so a reviewer can
    audit what the model was shown. It has no conversion path into a
    `ClaimRecord`: no method here mints one, no claim field accepts one, and
    validating it against the union fails because it declares no source class.
    Prose becomes policy only through a deterministic extractor (spec §C1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    source_range: str = Field(min_length=1)
    text: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# the claim union
# ---------------------------------------------------------------------------


class _BaseClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClaimKind
    typed_value: dict[str, Any]
    applicability: Applicability = Applicability()
    source_status: SourceStatus = "current"
    evidence_status: EvidenceStatus = "untested"

    @property
    def support_claim_ids(self) -> tuple[str, ...]:
        """The claims this one rests on — empty unless the ref names a set."""
        return ()

    @property
    def claim_id(self) -> str:
        return claim_id(self.kind, self.source_ref)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "claim_id": self.claim_id,
            "kind": self.kind,
            "typed_value": dict(self.typed_value),
            "source_class": self.source_class,
            "source_ref": self.source_ref.model_dump(mode="json"),
            "source_status": self.source_status,
            "evidence_status": self.evidence_status,
        }
        applicability = self.applicability.payload()
        if applicability:
            body["applicability"] = applicability
        if self.support_claim_ids:
            body["support_claim_ids"] = list(self.support_claim_ids)
        return body


class PolicyClaim(_BaseClaim):
    """What a repository document or configuration file STATES (spec §C1)."""

    source_class: Literal["repository_doc", "config"]
    source_ref: PolicySourceRef
    extraction_method: str = Field(min_length=1)

    @model_validator(mode="after")
    def _documentation_states_no_capability(self) -> PolicyClaim:
        if self.kind == "capability":
            raise ValueError(
                "documentation alone can never state a capability; a capability "
                "claim needs physical, receipt or inferred evidence"
            )
        return self

    def payload(self) -> dict[str, Any]:
        body = super().payload()
        body["extraction_method"] = self.extraction_method
        return body


class PhysicalClaim(_BaseClaim):
    """What a bounded probe OBSERVED on this machine."""

    source_class: Literal["physical"]
    source_ref: PhysicalSourceRef


class ReceiptClaim(_BaseClaim):
    """What one assessed invocation receipt established."""

    source_class: Literal["receipt"]
    source_ref: ReceiptSourceRef


class InferredClaim(_BaseClaim):
    """What a deterministic rule concluded from a complete support set."""

    source_class: Literal["inferred"]
    source_ref: InferredSourceRef

    @property
    def support_claim_ids(self) -> tuple[str, ...]:
        return tuple(self.source_ref.support_claim_ids)


ClaimRecord = Annotated[
    Union[PolicyClaim, PhysicalClaim, ReceiptClaim, InferredClaim],
    Field(discriminator="source_class"),
]
# Spec §C1: `CapabilityClaim` is not a fifth variant — it is the set of claim
# types that MAY carry `kind="capability"`. The documentation variant is
# excluded by construction here and refused by validation in `PolicyClaim`.
CapabilityClaim = Annotated[
    Union[PhysicalClaim, ReceiptClaim, InferredClaim],
    Field(discriminator="source_class"),
]

_CLAIM_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClaimRecord)


def parse_claim(payload: Mapping[str, Any]) -> Any:
    """Validate one persisted claim body back into its typed variant."""
    body = {
        key: value
        for key, value in dict(payload).items()
        if key not in ("schema_version", "claim_id", "support_claim_ids")
    }
    return _CLAIM_ADAPTER.validate_python(body)


# ---------------------------------------------------------------------------
# extraction surfaces
# ---------------------------------------------------------------------------


class _CommandLine(NamedTuple):
    """One shell-shaped line an entry contributes, with the context around it."""

    start_line: int
    end_line: int
    command: str
    surface: str
    cwd_hint: str | None = None
    workflow_job: str | None = None
    dockerfile_stage: str | None = None


@dataclass(frozen=True)
class _Draft:
    """A claim before it is given a source range — see `_mint`."""

    kind: str
    start_line: int
    end_line: int
    typed_value: dict[str, Any]
    extraction_method: str
    applicability: Applicability


def _lines(text: Any) -> list[str]:
    return str(text or "").splitlines()


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _entry_kind(entry: Mapping[str, Any]) -> str:
    return str(entry.get("kind") or "").strip().lower()


def _basename(entry: Mapping[str, Any]) -> str:
    return posixpath.basename(str(entry.get("path") or ""))


def _is_markdownish(entry: Mapping[str, Any]) -> bool:
    return _entry_kind(entry) in MARKDOWN_KINDS


def _is_yaml(entry: Mapping[str, Any]) -> bool:
    return _entry_kind(entry) in ("yaml", "yml")


def _is_dockerfile(entry: Mapping[str, Any]) -> bool:
    return _entry_kind(entry) in ("dockerfile", "docker") or _basename(entry).startswith(
        "Dockerfile"
    )


def _is_shell(entry: Mapping[str, Any]) -> bool:
    return _entry_kind(entry) in ("shell", "sh", "bash")


def _is_cmake(entry: Mapping[str, Any]) -> bool:
    return _entry_kind(entry) == "cmake" or _basename(entry) in ("CMakeLists.txt",)


def _is_requirements(entry: Mapping[str, Any]) -> bool:
    return bool(_REQUIREMENTS_FILE.match(_basename(entry)))


def _source_class(entry: Mapping[str, Any]) -> str:
    """`repository_doc` for prose, `config` for everything a tool reads.

    A requirements file has a prose-ish kind (`text`) but is machine input, so
    it is classified by what it IS rather than by how it is stored.
    """
    if _is_markdownish(entry) and not _is_requirements(entry):
        return "repository_doc"
    return "config"


def _markdown_command_lines(lines: Sequence[str]) -> list[_CommandLine]:
    """Every line inside a shell-shaped fenced block, with its `cd` context."""
    commands: list[_CommandLine] = []
    inside = False
    recording = False
    cwd_hint: str | None = None
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if inside:
                inside = recording = False
                cwd_hint = None
            else:
                inside = True
                info = stripped.lstrip("`~").strip().split()
                recording = (info[0].lower() if info else "") in SHELL_FENCE_LANGUAGES
                cwd_hint = None
            continue
        if not inside or not recording or not stripped:
            continue
        command = stripped.lstrip("$% ").strip() if stripped[0] in "$%" else stripped
        directory = re.match(r"^cd\s+(?P<path>\S+)$", command)
        if directory:
            cwd_hint = directory.group("path")
            continue
        commands.append(_CommandLine(number, number, command, "markdown_fenced_command", cwd_hint))
    return commands


def _markdown_prose_lines(lines: Sequence[str]) -> list[tuple[int, str]]:
    """Every line OUTSIDE a fenced block — where prose constraints live."""
    prose: list[tuple[int, str]] = []
    inside = False
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            inside = not inside
            continue
        if not inside and stripped:
            prose.append((number, raw))
    return prose


def _yaml_job_by_line(lines: Sequence[str]) -> dict[int, str]:
    """Which workflow job each line belongs to, by indentation alone.

    Line-shaped rather than parsed: the map must survive workflow files this
    harness cannot fully evaluate (reusable calls, anchors), and a job name is
    the only fact needed here.
    """
    jobs: dict[int, str] = {}
    jobs_indent: int | None = None
    key_indent: int | None = None
    current: str | None = None
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if current:
                jobs[number] = current
            continue
        indent = _indent(raw)
        if jobs_indent is None:
            if re.match(r"^jobs:\s*$", stripped):
                jobs_indent = indent
            continue
        if indent <= jobs_indent:
            jobs_indent = key_indent = None
            current = None
            continue
        match = re.match(r"^(?P<name>[A-Za-z_][A-Za-z0-9_.-]*):\s*$", stripped)
        if match and (key_indent is None or indent == key_indent):
            key_indent = indent
            current = match.group("name")
        if current:
            jobs[number] = current
    return jobs


def _yaml_list_items(lines: Sequence[str]) -> list[list[tuple[int, str]]]:
    """Group lines into `- ` list items, so a step's keys stay together."""
    items: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] | None = None
    current_indent: int | None = None
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            if current is not None:
                current.append((number, raw))
            continue
        indent = _indent(raw)
        if re.match(r"^\s*-\s", raw) and (current_indent is None or indent <= current_indent):
            current = [(number, raw)]
            current_indent = indent
            items.append(current)
            continue
        if current is not None and current_indent is not None and indent > current_indent:
            current.append((number, raw))
        else:
            current = None
            current_indent = None
    return items


def _yaml_scalar(block: Sequence[tuple[int, str]], key: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:-\s+)?{re.escape(key)}:\s*(?P<value>.+?)\s*$")
    for _, raw in block:
        match = pattern.match(raw)
        if match:
            return _unquote(match.group("value"))
    return None


def _yaml_run_commands(block: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """Every shell line a `run:` key contributes, inline or block scalar."""
    commands: list[tuple[int, str]] = []
    index = 0
    while index < len(block):
        number, raw = block[index]
        index += 1
        if not re.match(r"^\s*(?:-\s+)?run:", raw):
            continue
        key_indent = raw.index("run:")
        value = raw.split("run:", 1)[1].strip()
        if value in _YAML_BLOCK_SCALAR:
            while index < len(block):
                next_number, next_raw = block[index]
                if not next_raw.strip():
                    index += 1
                    continue
                if _indent(next_raw) <= key_indent:
                    break
                commands.append((next_number, next_raw.strip()))
                index += 1
            continue
        if value:
            commands.append((number, _unquote(value)))
    return commands


def _yaml_env_entries(lines: Sequence[str]) -> list[tuple[int, str, str]]:
    """`(line, name, value)` for every key under an `env:` mapping."""
    entries: list[tuple[int, str, str]] = []
    env_indent: int | None = None
    entry_indent: int | None = None
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent(raw)
        if re.match(r"^\s*(?:-\s+)?env:\s*$", raw):
            env_indent = raw.index("env:")
            entry_indent = None
            continue
        if env_indent is None:
            continue
        if indent <= env_indent:
            env_indent = entry_indent = None
            continue
        match = _YAML_KEY.match(raw)
        if not match:
            continue
        if entry_indent is None:
            entry_indent = indent
        if indent != entry_indent:
            continue
        value = _unquote(match.group("value"))
        if value:
            entries.append((number, match.group("key"), value))
    return entries


def _dockerfile_command_lines(lines: Sequence[str]) -> list[_CommandLine]:
    """Every `RUN` instruction, continuations joined, with its build stage."""
    commands: list[_CommandLine] = []
    stage: str | None = None
    index = 0
    while index < len(lines):
        start = index + 1
        parts = [lines[index].strip()]
        while parts[-1].endswith("\\") and index + 1 < len(lines):
            parts[-1] = parts[-1][:-1].strip()
            index += 1
            parts.append(lines[index].strip())
        end = index + 1
        index += 1
        joined = " ".join(part for part in parts if part)
        if not joined or joined.startswith("#"):
            continue
        from_instruction = _DOCKERFILE_FROM.match(joined)
        if from_instruction:
            stage = from_instruction.group("stage")
            continue
        run_instruction = _DOCKERFILE_RUN.match(joined)
        if run_instruction:
            commands.append(
                _CommandLine(
                    start,
                    end,
                    run_instruction.group("command").strip(),
                    "dockerfile_run",
                    dockerfile_stage=stage,
                )
            )
    return commands


def _shell_command_lines(lines: Sequence[str]) -> list[_CommandLine]:
    """Logical shell commands: backslash continuations join their opener.

    Live tvm r3: `pip3 install \\` + `    numpy==1.26.* \\` are ONE command in
    shell semantics; reading the continuation as its own line hid the pin from
    the dependency extractor and fed `numpy==1.26.*` to the env-assignment
    parser as a mangled assignment.
    """
    commands: list[_CommandLine] = []
    pending: Optional[list] = None  # [start, end, parts]
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if pending is not None:
            if not stripped or stripped.startswith("#"):
                start, end, parts = pending
                commands.append(_CommandLine(start, end, " ".join(parts), "shell_command"))
                pending = None
                continue
            if stripped.endswith("\\"):
                pending[1] = number
                pending[2].append(stripped[:-1].strip())
            else:
                start, _, parts = pending
                parts.append(stripped)
                commands.append(_CommandLine(start, number, " ".join(parts), "shell_command"))
                pending = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending = [number, number, [stripped[:-1].strip()]]
            continue
        commands.append(_CommandLine(number, number, stripped, "shell_command"))
    if pending is not None:
        start, end, parts = pending
        commands.append(_CommandLine(start, end, " ".join(parts), "shell_command"))
    return commands


def _command_lines(entry: Mapping[str, Any], text: str) -> list[_CommandLine]:
    """Every shell-shaped line this entry contributes, with its context."""
    lines = _lines(text)
    if _is_dockerfile(entry):
        return _dockerfile_command_lines(lines)
    if _is_yaml(entry):
        jobs = _yaml_job_by_line(lines)
        commands: list[_CommandLine] = []
        for block in _yaml_list_items(lines):
            cwd_hint = _yaml_scalar(block, "working-directory")
            for number, command in _yaml_run_commands(block):
                commands.append(
                    _CommandLine(number, number, command, "ci_run_step", cwd_hint, jobs.get(number))
                )
        return commands
    if _is_markdownish(entry) and not _is_requirements(entry):
        return _markdown_command_lines(lines)
    if _is_shell(entry):
        return _shell_command_lines(lines)
    return []


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return str(command or "").split()


def _runner_key(token: str) -> str:
    return posixpath.basename(str(token or "").strip()).lower()


def _resolve_cwd(checkout_root: str, hint: str | None) -> str:
    """The directory a documented command runs in.

    Only an explicit context statement moves it. An argument that happens to
    name a directory (`-f ./x/pom.xml`, `-C build`) never does — the Bigtop
    anchor documents a repository-root invocation and that is what is recorded.
    """
    root = str(checkout_root or "").rstrip("/") or "/"
    if not hint:
        return root
    if hint.startswith("/"):
        return posixpath.normpath(hint)
    return posixpath.normpath(posixpath.join(root, hint))


def _domain_for(path: str, domain_roots: Sequence[str]) -> str | None:
    """The longest domain root containing `path`, or None.

    Roots and entry paths must be expressed in the same coordinate system;
    this function never rewrites either into the other.
    """
    target = str(path or "")
    best: str | None = None
    for root in domain_roots or ():
        candidate = str(root or "").rstrip("/")
        if not candidate:
            continue
        if target == candidate or target.startswith(candidate + "/"):
            if best is None or len(candidate) > len(best):
                best = candidate
    return best


def _applicability(
    entry: Mapping[str, Any],
    *,
    domain_roots: Sequence[str] = (),
    workflow_job: str | None = None,
    dockerfile_stage: str | None = None,
) -> Applicability:
    return Applicability(
        domain=_domain_for(str(entry.get("path") or ""), domain_roots),
        workflow_job=workflow_job or None,
        dockerfile_stage=dockerfile_stage or None,
    )


def _mint(entry: Mapping[str, Any], drafts: Sequence[_Draft]) -> list[PolicyClaim]:
    """Give each draft its source range and turn it into a `PolicyClaim`.

    Ranges are assigned here rather than in the extractors because uniqueness
    is a property of the WHOLE pass: `-DUSE_LLVM=ON -DUSE_CUDA=OFF` on one
    line yields three env claims, and a claim id is a digest of its range, so
    the occurrence index has to be handed out once, in order, at the end.
    """
    entry_id = str(entry.get("entry_id") or "").strip()
    source_hash = str(entry.get("source_hash") or "").strip()
    if not entry_id or not source_hash:
        if drafts:
            logger.debug(
                f"document entry {entry.get('path')!r} states no id/source hash; "
                f"{len(drafts)} extracted claims have no provenance and were dropped"
            )
        return []
    source_class = _source_class(entry)
    occupancy = Counter((draft.kind, draft.start_line, draft.end_line) for draft in drafts)
    seen: Counter[tuple[str, int, int]] = Counter()
    claims: list[PolicyClaim] = []
    for draft in drafts:
        key = (draft.kind, draft.start_line, draft.end_line)
        occurrence = seen[key]
        seen[key] += 1
        span = (
            f"L{draft.start_line}"
            if draft.start_line == draft.end_line
            else f"L{draft.start_line}-L{draft.end_line}"
        )
        if occupancy[key] > 1:
            span = f"{span}#{occurrence}"
        claims.append(
            PolicyClaim(
                kind=draft.kind,
                typed_value=draft.typed_value,
                source_class=source_class,
                source_ref=PolicySourceRef(
                    entry_id=entry_id, source_hash=source_hash, source_range=span
                ),
                extraction_method=draft.extraction_method,
                applicability=draft.applicability,
            )
        )
    return claims


def _line_of(text: str, index: int) -> int:
    return str(text or "").count("\n", 0, index) + 1


# ---------------------------------------------------------------------------
# extractors
# ---------------------------------------------------------------------------


def extract_tool_constraints(
    entry: Mapping[str, Any], text: str, *, domain_roots: Sequence[str] = ()
) -> list[PolicyClaim]:
    """Tool version constraints stated as LITERALS (spec §C1).

    Two surfaces: the maven-enforcer `require*Version` rules, and prose that
    names a tool next to a version literal. Prose without a literal — "you
    need Maven and a JDK" — states no constraint and extracts nothing.
    """
    drafts: list[_Draft] = []
    applicability = _applicability(entry, domain_roots=domain_roots)
    if _entry_kind(entry) in ("xml", "pom"):
        for match in _MAVEN_REQUIRE_VERSION.finditer(str(text or "")):
            drafts.append(
                _Draft(
                    kind="tool_constraint",
                    start_line=_line_of(text, match.start()),
                    end_line=_line_of(text, match.end()),
                    typed_value={
                        "tool": match.group("tool").lower(),
                        "constraint": match.group("constraint").strip(),
                    },
                    extraction_method="maven_enforcer_require_version",
                    applicability=applicability,
                )
            )
    if _is_markdownish(entry) and not _is_requirements(entry):
        prose = (
            _markdown_prose_lines(_lines(text))
            if _entry_kind(entry) in ("markdown", "md", "rst")
            else list(enumerate(_lines(text), start=1))
        )
        for number, line in prose:
            for match in _PROSE_TOOL_VERSION.finditer(line):
                tool = _PROSE_TOOL_NAMES.get(match.group("tool").lower())
                if tool is None:
                    continue
                constraint = f"{match.group('operator') or ''}{match.group('version')}"
                drafts.append(
                    _Draft(
                        kind="tool_constraint",
                        start_line=number,
                        end_line=number,
                        typed_value={"tool": tool, "constraint": constraint},
                        extraction_method="markdown_prose_version_literal",
                        applicability=applicability,
                    )
                )
    return _mint(entry, drafts)


def extract_lifecycle_commands(
    entry: Mapping[str, Any],
    text: str,
    *,
    checkout_root: str,
    domain_roots: Sequence[str] = (),
) -> list[PolicyClaim]:
    """Documented build/test commands, argv verbatim and cwd from context.

    `checkout_root` is required rather than defaulted: a documented command
    whose directory is a guess is worse than no claim at all.
    """
    drafts: list[_Draft] = []
    for line in _command_lines(entry, text):
        argv = _tokenize(line.command)
        if not argv:
            continue
        tool = LIFECYCLE_RUNNERS.get(_runner_key(argv[0]))
        if tool is None:
            continue
        drafts.append(
            _Draft(
                kind="lifecycle",
                start_line=line.start_line,
                end_line=line.end_line,
                typed_value={
                    "tool": tool,
                    "argv": argv,
                    "cwd": _resolve_cwd(checkout_root, line.cwd_hint),
                },
                extraction_method=line.surface,
                applicability=_applicability(
                    entry,
                    domain_roots=domain_roots,
                    workflow_job=line.workflow_job,
                    dockerfile_stage=line.dockerfile_stage,
                ),
            )
        )
    return _mint(entry, drafts)


def _pip_pins(argv: Sequence[str]) -> list[dict[str, str]]:
    """`pip install` arguments that carry a version literal.

    An unpinned `pip install numpy` states an intention, not a constraint, so
    it is not recorded here; an apt package literal is recorded because the
    literal IS the pin (`llvm-14` names its version).
    """
    pins: list[dict[str, str]] = []
    index = 0
    while index < len(argv):
        if _runner_key(argv[index]) not in ("pip", "pip3"):
            index += 1
            continue
        cursor = index + 1
        installing = False
        while cursor < len(argv):
            token = argv[cursor]
            cursor += 1
            if token in SHELL_OPERATORS:
                break
            if not installing:
                installing = token == "install"
                continue
            if token.startswith("-"):
                continue
            match = _PIP_REQUIREMENT.match(token)
            if match:
                pins.append(
                    {
                        "ecosystem": "pip",
                        "package": match.group("package"),
                        "specifier": match.group("specifier"),
                        "version": match.group("version"),
                    }
                )
        index = cursor
    return pins


def _apt_packages(argv: Sequence[str]) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    index = 0
    while index < len(argv):
        if _runner_key(argv[index]) not in ("apt", "apt-get"):
            index += 1
            continue
        cursor = index + 1
        installing = False
        while cursor < len(argv):
            token = argv[cursor]
            cursor += 1
            if token in SHELL_OPERATORS:
                break
            if not installing:
                installing = token == "install"
                continue
            if token.startswith("-"):
                continue
            match = _APT_PACKAGE.match(token)
            if not match:
                continue
            package = {"ecosystem": "apt", "package": match.group("package")}
            if match.group("specifier"):
                package["specifier"] = match.group("specifier")
                package["version"] = match.group("version")
            packages.append(package)
        index = cursor
    return packages


def extract_dependency_pins(
    entry: Mapping[str, Any], text: str, *, domain_roots: Sequence[str] = ()
) -> list[PolicyClaim]:
    """Dependency literals: pip pins, requirements lines, Docker RUN packages."""
    drafts: list[_Draft] = []
    if _is_requirements(entry):
        applicability = _applicability(entry, domain_roots=domain_roots)
        for number, raw in enumerate(_lines(text), start=1):
            line = raw.split("#", 1)[0].split(";", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _PIP_REQUIREMENT.match(line)
            if not match:
                continue
            drafts.append(
                _Draft(
                    kind="dependency",
                    start_line=number,
                    end_line=number,
                    typed_value={
                        "ecosystem": "pip",
                        "package": match.group("package"),
                        "specifier": match.group("specifier"),
                        "version": match.group("version"),
                    },
                    extraction_method="requirements_line",
                    applicability=applicability,
                )
            )
        return _mint(entry, drafts)
    for line in _command_lines(entry, text):
        argv = _tokenize(line.command)
        if not argv:
            continue
        applicability = _applicability(
            entry,
            domain_roots=domain_roots,
            workflow_job=line.workflow_job,
            dockerfile_stage=line.dockerfile_stage,
        )
        for typed_value in _apt_packages(argv) + _pip_pins(argv):
            drafts.append(
                _Draft(
                    kind="dependency",
                    start_line=line.start_line,
                    end_line=line.end_line,
                    typed_value=typed_value,
                    extraction_method=line.surface,
                    applicability=applicability,
                )
            )
    return _mint(entry, drafts)


def _environment_drafts(
    name: str,
    value: str,
    *,
    start_line: int,
    end_line: int,
    extraction_method: str,
    applicability: Applicability,
) -> list[_Draft]:
    """One env draft, plus one per `-DNAME=VALUE` when the variable is CMAKE_ARGS."""
    drafts = [
        _Draft(
            kind="env",
            start_line=start_line,
            end_line=end_line,
            typed_value={"scope": "environment", "name": name, "value": value},
            extraction_method=extraction_method,
            applicability=applicability,
        )
    ]
    if name != "CMAKE_ARGS":
        return drafts
    for match in _CMAKE_DEFINITION.finditer(value):
        drafts.append(
            _Draft(
                kind="env",
                start_line=start_line,
                end_line=end_line,
                typed_value={
                    "scope": "cmake_definition",
                    "name": match.group("name"),
                    "value": match.group("value"),
                },
                extraction_method="cmake_args",
                applicability=applicability,
            )
        )
    return drafts


def extract_env_definitions(
    entry: Mapping[str, Any], text: str, *, domain_roots: Sequence[str] = ()
) -> list[PolicyClaim]:
    """Environment and CMake definitions stated as literals.

    A value containing `$` or a backtick is an expansion, not a literal, and
    is skipped — recording `CACHE_DIR=$HOME/.cache` as a fact would state a
    path this survey has not seen.
    """
    drafts: list[_Draft] = []
    lines = _lines(text)
    for line in _command_lines(entry, text):
        assignment = _SHELL_ASSIGNMENT.match(line.command)
        if not assignment:
            continue
        value = _unquote(assignment.group("value"))
        if not value or "$" in value or "`" in value:
            continue
        drafts.extend(
            _environment_drafts(
                assignment.group("name"),
                value,
                start_line=line.start_line,
                end_line=line.end_line,
                extraction_method="shell_assignment",
                applicability=_applicability(
                    entry,
                    domain_roots=domain_roots,
                    workflow_job=line.workflow_job,
                    dockerfile_stage=line.dockerfile_stage,
                ),
            )
        )
    if _is_yaml(entry):
        jobs = _yaml_job_by_line(lines)
        for number, name, value in _yaml_env_entries(lines):
            if "$" in value or "`" in value:
                continue
            drafts.extend(
                _environment_drafts(
                    name,
                    value,
                    start_line=number,
                    end_line=number,
                    extraction_method="ci_env_mapping",
                    applicability=_applicability(
                        entry, domain_roots=domain_roots, workflow_job=jobs.get(number)
                    ),
                )
            )
    if _is_cmake(entry):
        applicability = _applicability(entry, domain_roots=domain_roots)
        for number, raw in enumerate(lines, start=1):
            for pattern, scope, method in (
                (_CMAKE_SET, "cmake_set", "cmake_set"),
                (_CMAKE_OPTION, "cmake_option", "cmake_option"),
            ):
                match = pattern.match(raw)
                if not match:
                    continue
                value = match.group("value")
                if "$" in value:
                    continue
                drafts.append(
                    _Draft(
                        kind="env",
                        start_line=number,
                        end_line=number,
                        typed_value={
                            "scope": scope,
                            "name": match.group("name"),
                            "value": value,
                        },
                        extraction_method=method,
                        applicability=applicability,
                    )
                )
    return _mint(entry, drafts)


def entry_has_extractors(entry: Mapping[str, Any]) -> bool:
    """Whether any extractor here can read this document map entry.

    The kind decides, with the three name-carried exceptions the extractors
    themselves already honour (a requirements file, a Dockerfile and a
    CMakeLists are recognised by what they ARE, not by how they are stored).
    A False answer means fetching this entry's text would buy nothing.
    """
    return (
        _entry_kind(entry) in EXTRACTOR_ENTRY_KINDS
        or _is_requirements(entry)
        or _is_dockerfile(entry)
        or _is_cmake(entry)
    )


def extract_policy_claims(
    entry: Mapping[str, Any],
    text: str,
    *,
    checkout_root: str,
    domain_roots: Sequence[str] = (),
) -> list[PolicyClaim]:
    """Every deterministic claim one document map entry states."""
    claims: list[PolicyClaim] = []
    claims.extend(extract_tool_constraints(entry, text, domain_roots=domain_roots))
    claims.extend(
        extract_lifecycle_commands(
            entry, text, checkout_root=checkout_root, domain_roots=domain_roots
        )
    )
    claims.extend(extract_dependency_pins(entry, text, domain_roots=domain_roots))
    claims.extend(extract_env_definitions(entry, text, domain_roots=domain_roots))
    return claims


# ---------------------------------------------------------------------------
# equal-applicability conflicts
# ---------------------------------------------------------------------------


def _conflict_subject(claim: Any) -> tuple[str, ...] | None:
    """What a claim is ABOUT, so only claims about the same thing can disagree.

    Two pins in one requirements file are not a conflict; two different maven
    version ranges for the same domain are. Without a subject the rule would
    report every pair of unrelated facts (spec §6 "Source conflict").
    """
    value = dict(claim.typed_value or {})
    if claim.kind == "tool_constraint":
        subject = value.get("tool")
    elif claim.kind == "dependency":
        subject = f"{value.get('ecosystem')} {value.get('package')}"
    elif claim.kind == "env":
        subject = value.get("name")
    elif claim.kind == "lifecycle":
        subject = f"{value.get('tool')} lifecycle command"
    elif claim.kind == "capability":
        subject = value.get("capability")
    else:
        subject = None
    if not subject:
        return None
    return (claim.kind, str(subject))


def find_claim_conflicts(claims: Iterable[Any]) -> list[dict[str, Any]]:
    """One `claim_conflict` record per equally-applicable disagreement.

    Both claims are KEPT. The harness never picks the source that happens to
    make an anchor green (spec §C1); the disagreement stays visible and the
    domain carries it as an open conflict.
    """
    groups: dict[tuple[str, ...], list[Any]] = {}
    for claim in claims:
        subject = _conflict_subject(claim)
        if subject is None:
            continue
        key = subject + (json.dumps(claim.applicability.payload(), sort_keys=True),)
        groups.setdefault(key, []).append(claim)
    conflicts: list[dict[str, Any]] = []
    for key, group in groups.items():
        distinct = {json.dumps(claim.typed_value, sort_keys=True) for claim in group}
        if len(distinct) < 2:
            continue
        detail = (
            f"{len(distinct)} different values for {key[1]} " f"under one applicability ({key[0]})"
        )
        conflicts.append(
            {
                "kind": "claim_conflict",
                "claim_ids": sorted({claim.claim_id for claim in group}),
                "detail": " ".join(detail.split())[:DETAIL_MAX_CHARS],
            }
        )
    return sorted(conflicts, key=lambda record: record["claim_ids"])


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def write_claims(execute: Callable[..., Any], claims: Iterable[Any]) -> bool:
    """Persist every claim atomically; True when all of them are on disk.

    The comprehension is materialized before `all` so one refused claim does
    not short-circuit the rest: a partial survey must still leave every claim
    it CAN persist on disk, and the caller learns that something was refused.
    """
    return all([write_claim(execute, claim) for claim in claims])


def write_claim(execute: Callable[..., Any], claim: Any) -> bool:
    """Persist one claim; True when the file holds exactly this body.

    Same contract as `write_assessment`: the same body under an existing id is
    a no-op success (replay must not double-write), a DIFFERENT body under an
    existing id is refused and logged (an id is a claim about identity, so a
    collision is a defect to see, not to resolve silently), and the write is
    temp-file + `mv` so no reader ever sees half a claim.
    """
    payload = claim.payload()
    identifier = payload["claim_id"]
    try:
        body = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug(f"claim {identifier} is not serializable: {exc}")
        return False
    final = f"{CLAIM_DIR}/{identifier}.json"
    existing = _read_existing(execute, final)
    if existing is not None:
        if existing == payload:
            return True
        logger.warning(
            f"claim {identifier} already records a different body; claims are "
            "written once per source and this write was refused"
        )
        return False
    temp = f"{final}.tmp"
    command = (
        f"mkdir -p {shlex.quote(CLAIM_DIR)} && "
        f"cat > {shlex.quote(temp)} <<'{CLAIM_HEREDOC}' && "
        f"mv -f {shlex.quote(temp)} {shlex.quote(final)}\n"
        f"{body}\n{CLAIM_HEREDOC}"
    )
    try:
        result = execute(command) or {}
    except Exception as exc:
        logger.debug(f"claim {identifier} not persisted: {exc}")
        return False
    return _succeeded(result)


def _read_existing(execute: Callable[..., Any], path: str) -> dict[str, Any] | None:
    """The claim already at `path`, or None when there is none to honour.

    An unparseable file is reported as a body that matches nothing, so the
    caller refuses instead of overwriting bytes it cannot account for.
    """
    try:
        result = execute(f"cat {shlex.quote(path)}") or {}
    except Exception as exc:
        logger.debug(f"claim {path} unreadable: {exc}")
        return None
    content = str(result.get("output") or "").strip()
    if not _succeeded(result) or not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {"unparseable": path}
    return payload if isinstance(payload, dict) else {"unparseable": path}


def _succeeded(result: Mapping[str, Any]) -> bool:
    """Container results state either `success` or an exit code; accept both."""
    success = (result or {}).get("success")
    if success is None:
        success = (result or {}).get("exit_code") == 0
    return bool(success)


__all__ = [
    "CLAIM_DIR",
    "CLAIM_HEREDOC",
    "CLAIM_KINDS",
    "CLAIM_SCHEMA_VERSION",
    "EVIDENCE_STATUSES",
    "LIFECYCLE_RUNNERS",
    "SOURCE_CLASSES",
    "SOURCE_STATUSES",
    "Applicability",
    "CapabilityClaim",
    "ClaimRecord",
    "InferredClaim",
    "InferredSourceRef",
    "PhysicalClaim",
    "PhysicalSourceRef",
    "PolicyClaim",
    "PolicySourceRef",
    "ReceiptClaim",
    "ReceiptSourceRef",
    "UntrustedDocInterpretation",
    "claim_id",
    "extract_dependency_pins",
    "extract_env_definitions",
    "extract_lifecycle_commands",
    "extract_policy_claims",
    "extract_tool_constraints",
    "find_claim_conflicts",
    "parse_claim",
    "write_claim",
    "write_claims",
]
