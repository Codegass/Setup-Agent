"""Canonical per-test identities and explicit-attempt result histories."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import ClassVar, Iterable, Literal

TestStatus = Literal["passed", "skipped", "failed", "error"]

_STATUS_SEVERITY: dict[str, int] = {
    "passed": 0,
    "skipped": 1,
    "failed": 2,
    "error": 3,
}


def normalize_test_status(value: str) -> TestStatus:
    status = str(value or "").strip().lower()
    if status not in _STATUS_SEVERITY:
        raise ValueError(f"unsupported test status: {value!r}")
    return status  # type: ignore[return-value]


def worst_test_status(left: str, right: str) -> TestStatus:
    current = normalize_test_status(left)
    candidate = normalize_test_status(right)
    return candidate if _STATUS_SEVERITY[candidate] > _STATUS_SEVERITY[current] else current


def _normalized_file_path(value: str | None) -> str:
    if not value:
        return ""
    path = str(value).strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    while path.startswith("./"):
        path = path[2:]
    # Container roots are run-specific transport, not test identity.
    if path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
    return path.rstrip("/")


def _name_and_param_id(value: str) -> tuple[str, str]:
    name = str(value or "").strip()
    if not name:
        return "", ""

    param_id = ""
    bracket = re.search(r"\[([^\]]*)\]\s*$", name)
    if bracket:
        param_id = bracket.group(1).strip()
        name = name[: bracket.start()].rstrip()

    # JUnit 5 may expose a method signature before the trailing invocation id.
    if "(" in name:
        name = name.split("(", 1)[0].rstrip()
    spock = re.search(r"\s+#([^\s]+)\s*$", name)
    if spock:
        param_id = param_id or spock.group(1).strip()
        name = name[: spock.start()].rstrip()
    return name, param_id


@dataclass(frozen=True, order=True, slots=True)
class CanonicalTestIdentity:
    module_or_file: str
    class_name: str
    name: str
    param_id: str = ""

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.module_or_file, self.class_name, self.name, self.param_id)

    @property
    def key(self) -> str:
        return json.dumps(self.as_tuple(), ensure_ascii=True, separators=(",", ":"))

    @property
    def method_key(self) -> str:
        return json.dumps(
            (self.module_or_file, self.class_name, self.name),
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @property
    def display_name(self) -> str:
        if "/" in self.module_or_file or self.module_or_file.endswith((".py", ".java")):
            owner = "::".join(part for part in (self.module_or_file, self.class_name) if part)
        else:
            owner = ".".join(part for part in (self.module_or_file, self.class_name) if part)
        prefix = "::".join(part for part in (owner, self.name) if part)
        return f"{prefix}[{self.param_id}]" if self.param_id else prefix

    def to_dict(self) -> dict[str, str]:
        return {
            "module_or_file": self.module_or_file,
            "class_name": self.class_name,
            "name": self.name,
            "param_id": self.param_id,
        }


def canonical_test_identity(
    classname: str | None,
    name: str | None,
    file_path: str | None = None,
) -> CanonicalTestIdentity | None:
    normalized_name, param_id = _name_and_param_id(name or "")
    if not normalized_name:
        return None

    normalized_class = str(classname or "").strip().replace("$", ".").strip(".")
    normalized_file = _normalized_file_path(file_path)
    if normalized_file:
        module_or_file = normalized_file
    elif "." in normalized_class:
        module_or_file = normalized_class.rsplit(".", 1)[0]
    else:
        module_or_file = normalized_class

    class_name = normalized_class.rsplit(".", 1)[-1] if normalized_class else ""
    if not class_name and normalized_file:
        class_name = normalized_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    return CanonicalTestIdentity(
        module_or_file=module_or_file,
        class_name=class_name,
        name=normalized_name,
        param_id=param_id,
    )


@dataclass(frozen=True, slots=True)
class TestResultObservation:
    __test__: ClassVar[bool] = False

    identity: CanonicalTestIdentity
    attempt_id: int
    status: TestStatus | str
    source: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.attempt_id, bool) or int(self.attempt_id) < 1:
            raise ValueError("attempt_id must be a positive monotonic integer")
        object.__setattr__(self, "attempt_id", int(self.attempt_id))
        object.__setattr__(self, "status", normalize_test_status(self.status))


@dataclass(frozen=True, slots=True)
class TestResultHistory:
    __test__: ClassVar[bool] = False

    first: TestStatus
    latest: TestStatus
    worst: TestStatus
    retried_count: int
    attempt_ids: tuple[int, ...]
    sources: tuple[str, ...] = ()

    @property
    def flaky(self) -> bool:
        return self.latest == "passed" and self.worst in {"failed", "error"}

    def to_dict(self, identity: CanonicalTestIdentity) -> dict[str, object]:
        return {
            "identity": identity.to_dict(),
            "first": self.first,
            "latest": self.latest,
            "worst": self.worst,
            "retried_count": self.retried_count,
            "attempt_ids": list(self.attempt_ids),
            "flaky": self.flaky,
            "sources": list(self.sources),
        }


def _empty_counts() -> dict[str, int]:
    return {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}


def _bump(counts: dict[str, int], status: str) -> None:
    normalized = normalize_test_status(status)
    counts["executed"] += 1
    counts["errors" if normalized == "error" else normalized] += 1


@dataclass(frozen=True, slots=True)
class AggregatedTestResults:
    histories: dict[CanonicalTestIdentity, TestResultHistory]
    latest_counts: dict[str, int]
    raw_counts: dict[str, int]
    flaky_count: int
    retried_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "latest": dict(self.latest_counts),
            "raw": dict(self.raw_counts),
            "flaky_count": self.flaky_count,
            "retried_count": self.retried_count,
            "histories": [
                history.to_dict(identity) for identity, history in sorted(self.histories.items())
            ],
        }


def aggregate_test_results(
    observations: Iterable[TestResultObservation],
) -> AggregatedTestResults:
    by_identity: dict[CanonicalTestIdentity, dict[int, TestStatus]] = {}
    sources: dict[CanonicalTestIdentity, set[str]] = {}
    raw_counts = _empty_counts()

    for observation in observations:
        _bump(raw_counts, observation.status)
        attempts = by_identity.setdefault(observation.identity, {})
        existing = attempts.get(observation.attempt_id)
        attempts[observation.attempt_id] = (
            worst_test_status(existing, observation.status)
            if existing is not None
            else normalize_test_status(observation.status)
        )
        if observation.source:
            sources.setdefault(observation.identity, set()).add(observation.source)

    histories: dict[CanonicalTestIdentity, TestResultHistory] = {}
    latest_counts = _empty_counts()
    for identity, attempts in by_identity.items():
        ordered = tuple(sorted(attempts))
        statuses = tuple(attempts[attempt_id] for attempt_id in ordered)
        worst = statuses[0]
        for status in statuses[1:]:
            worst = worst_test_status(worst, status)
        history = TestResultHistory(
            first=statuses[0],
            latest=statuses[-1],
            worst=worst,
            retried_count=max(len(ordered) - 1, 0),
            attempt_ids=ordered,
            sources=tuple(sorted(sources.get(identity, set()))),
        )
        histories[identity] = history
        _bump(latest_counts, history.latest)

    return AggregatedTestResults(
        histories=histories,
        latest_counts=latest_counts,
        raw_counts=raw_counts,
        flaky_count=sum(history.flaky for history in histories.values()),
        retried_count=sum(history.retried_count for history in histories.values()),
    )


__all__ = [
    "AggregatedTestResults",
    "CanonicalTestIdentity",
    "TestResultHistory",
    "TestResultObservation",
    "TestStatus",
    "aggregate_test_results",
    "canonical_test_identity",
    "normalize_test_status",
    "worst_test_status",
]
