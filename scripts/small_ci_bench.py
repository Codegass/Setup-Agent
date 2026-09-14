#!/usr/bin/env python3
"""Freeze a small CI-anchored battery, then use the existing production runner.

The official API/console bytes are the benchmark, not a list of repository HEADs.
No network request or SAG invocation occurs on import. Targets use the existing
CellTarget and attainment implementation; this script does not invent a score.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shlex
import shutil
import signal
import tarfile
import tempfile
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from scripts import d3r2_campaign as runner
from scripts.d3_harvest_target import (
    HarvestError,
    IDENTITY_CHARACTER_BOUND,
    IDENTITY_COUNT_BOUND,
    cell_from_jenkins_report,
)
from sag.metrics.ci_logs import modules_from_log
from sag.metrics.target_record import CellTarget, TargetRecord

ROOT = Path(__file__).resolve().parents[1]


def pipeline_stage(console: str, stage: str) -> tuple[str, int, int]:
    """Select one named stage, preserving its original line boundaries."""
    lines = console.splitlines()
    marker = f"[Pipeline] {{ ({stage})"
    starts = [i for i, line in enumerate(lines) if line == marker]
    if len(starts) != 1:
        raise HarvestError("CI stage must occur exactly once")
    start = starts[0]
    # Declarative stages close with this marker after their nested shell blocks.
    ends = [i for i in range(start + 1, len(lines)) if lines[i] == "[Pipeline] // stage"]
    if not ends:
        raise HarvestError("CI stage is incomplete")
    end = ends[0]
    return "\n".join(lines[start : end + 1]) + "\n", start + 1, end + 1


def flat_jenkins_cell(
    report: dict, *, stage: str, stage_log: str, cell_id: str, command: str,
    refs: tuple[str, ...], stage_context: list[str] | None = None,
) -> CellTarget:
    """A complete Jenkins pipeline JUnit pool, tied to one recorded stage.

    FIXED is a pass compared with a previous build, not evidence of a flaky
    retry. Repeated display names remain separate final testcase occurrences.
    """
    suites = report.get("suites")
    if not isinstance(suites, list) or not suites:
        raise HarvestError("Pipeline JUnit testcase suites unavailable")
    counts: Counter[str] = Counter()
    occurrences: Counter[str] = Counter()
    identities, red = [], []
    for suite in suites:
        if suite.get("enclosingBlockNames") != (stage_context or [stage]) or not suite.get("nodeId"):
            raise HarvestError("JUnit pool belongs to a different or ambiguous pipeline stage")
        cases = suite.get("cases")
        if not isinstance(cases, list):
            raise HarvestError("JUnit suite omits testcase rows")
        for case in cases:
            outcome = {
                "PASSED": "passCount", "FIXED": "passCount",
                "FAILED": "failCount", "REGRESSION": "failCount", "SKIPPED": "skipCount",
            }.get(case.get("status"))
            if (
                outcome is None or not case.get("className") or not case.get("name")
                or bool(case.get("skipped", False)) != (outcome == "skipCount")
            ):
                raise HarvestError("Invalid Jenkins testcase identity or outcome")
            counts[outcome] += 1
            key = f"{case['className']}#{case['name']}"
            occurrences[key] += 1
            if occurrences[key] > 1:
                key += f" [duplicate-name {occurrences[key]}]"
            identities.append(key)
            if outcome == "failCount":
                red.append(key)
    if any(type(report.get(key)) is not int or report[key] != counts[key]
           for key in ("passCount", "failCount", "skipCount")):
        raise HarvestError("Pipeline totals contradict complete testcase rows")
    scope = modules_from_log(stage_log)
    if not scope.modules or scope.notes or scope.failed or scope.skipped:
        raise HarvestError("Selected CI stage has no complete successful build scope")

    def bounded(values: list[str]) -> tuple[str, ...]:
        return tuple(values) if len(values) <= IDENTITY_COUNT_BOUND and all(
            len(value) <= IDENTITY_CHARACTER_BOUND for value in values
        ) else ()

    return CellTarget(
        cell_id=cell_id, build="ok", executed_count=len(identities),
        executed_ids=bounded(identities), red_count=len(red), red_ids=bounded(red),
        skipped=counts["skipCount"], modules=scope.modules, modules_basis="log",
        command=command, grade="A", evidence_refs=refs,
    )


class NodeConsole(HTMLParser):
    """Extract the complete Jenkins node console, not wfapi's 10 KB tail."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.closed = False
        self.parts: list[str] = []
        self.found = 0

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and "console-output" in dict(attrs).get("class", "").split():
            self.inside = True
            self.found += 1

    def handle_endtag(self, tag):
        if tag == "pre" and self.inside:
            self.inside = False
            self.closed = True

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def result(self) -> str:
        if self.found != 1 or not self.closed:
            raise HarvestError("Node console HTML has no single complete console body")
        return "".join(self.parts)


def read_official(entry: dict, folder: Path) -> tuple[TargetRecord, dict]:
    """Validate one downloaded official build before admitting its target."""
    metadata = json.loads((folder / "build.json").read_text())
    report = json.loads((folder / "test-report.json").read_text())
    console = (folder / "console.log").read_text()
    provenance = json.loads((folder / "provenance.json").read_text())
    if metadata.get("url") != entry["build_url"] or provenance["build_url"] != entry["build_url"]:
        raise HarvestError("Official build URL differs from the selected build")
    if metadata.get("building") is not False or metadata.get("result") != "SUCCESS":
        raise HarvestError("Official build is not a completed successful run")
    if not re.search(r"^Finished: SUCCESS\s*$", console, re.M):
        raise HarvestError("Official console has no successful terminal boundary")
    log_shas = set(re.findall(r"Checking out Revision ([0-9a-f]{40})\b", console))
    repo_name = entry["repo"].split("/", 1)[1]
    scm = [a for a in metadata.get("actions", []) if a.get("lastBuiltRevision") and any(
        url.rstrip("/").removesuffix(".git").split("/")[-1] == repo_name
        for url in a.get("remoteUrls", [])
    )]
    api_shas = {a["lastBuiltRevision"]["SHA1"] for a in scm}
    if (
        entry["sha"] not in log_shas or api_shas != {entry["sha"]}
    ):
        raise HarvestError("CI repository/revision is missing, mixed, or mismatched")
    files = provenance["files"]
    for name in files:
        if runner.digest(folder / name) != files[name]["sha256"]:
            raise HarvestError("Official evidence bytes changed after download")
    cell_id = entry["cell_id"]
    refs = tuple(entry["build_url"] + suffix for suffix in (
        "api/json", "consoleText", "testReport/api/json"
    ))
    details = {"stage": entry.get("stage"), "counts_basis": "complete Jenkins final testcase rows"}
    selected_log = console
    if entry.get("stage"):
        if entry.get("shell_node"):
            node = json.loads((folder / "stage.json").read_text())
            shells = [n for n in node["stageFlowNodes"] if n["id"] == entry["shell_node"]]
            if len(shells) != 1 or shells[0]["status"] != "SUCCESS" or node["name"] != entry["stage"]:
                raise HarvestError("Selected shell is not a successful node of the selected stage")
            if shells[0]["parameterDescription"] != entry["official_command"]:
                raise HarvestError("Selected flow node has a different command")
            parser = NodeConsole()
            parser.feed((folder / "node-console.html").read_text())
            selected_log = parser.result()
            details.update(ci_shell_seconds=shells[0]["durationMillis"] / 1000,
                           stage_node=entry["stage_node"], shell_node=entry["shell_node"])
        else:
            selected_log, first, last = pipeline_stage(console, entry["stage"])
            details["console_lines"] = {"first": first, "last": last}
        if " ".join(shlex.split(entry["official_command"])) not in " ".join(selected_log.split()):
            raise HarvestError("Selected stage does not contain the frozen official command")
        cell = flat_jenkins_cell(
            report, stage=entry["stage"], stage_log=selected_log, cell_id=cell_id,
            command=entry["official_command"], refs=refs, stage_context=entry.get("stage_context"),
        )
    else:
        expected_suffix = re.sub(r"^mvn -B -f pom.xml ", "", entry["official_command"])
        if not any(line.startswith("Executing Maven:") and line.endswith(expected_suffix)
                   for line in console.splitlines()):
            raise HarvestError("Frozen Maven command differs from the official invocation")
        cell = cell_from_jenkins_report(
            report, cell_id=cell_id, build_url=entry["build_url"], console_text=console,
            command=entry["official_command"], evidence_refs=refs,
        ).cell
    maven_versions = set(re.findall(r"Apache Maven (\d+\.\d+\.\d+)\b", selected_log))
    if metadata.get("mavenVersionUsed"):
        maven_versions.add(metadata["mavenVersionUsed"])
    if maven_versions != {entry["maven_version"]}:
        raise HarvestError("Actual official Maven version differs from the frozen toolchain")
    java_versions = set(re.findall(r"Java version: ([^,\s]+)", selected_log))
    if java_versions:
        majors = {int(v.split(".")[1] if v.startswith("1.") else v.split(".")[0]) for v in java_versions}
    else:
        launchers = re.findall(r"^.*\] \$ (\S+/bin/java)\s", selected_log, re.M)
        major = re.search(r"(?:latest|jdk-)(\d+)", launchers[-1]) if launchers else None
        majors = {int(major.group(1))} if major else set()
    if majors != {entry["jdk_major"]}:
        raise HarvestError("Actual official JDK major is unavailable or mismatched")
    details["jdk_versions"] = sorted(java_versions) or None
    details["jdk_patch_known"] = bool(java_versions)
    pom = ET.fromstring((folder / "source-pom.xml").read_bytes())
    namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
    details["source_pom_version"] = pom.findtext("m:version", namespaces=namespace)
    if not cell.modules or cell.executed_count <= 0 or cell.red_count:
        raise HarvestError("A small-bench target needs build scope and a nonempty green test pool")
    details.update(
        reported=cell.executed_count, passed=cell.executed_count-cell.red_count-cell.skipped,
        red=cell.red_count, skipped=cell.skipped, executed_non_skipped=cell.executed_count-cell.skipped,
        build_modules=len(cell.modules), modules=list(cell.modules),
        ci_job_seconds=metadata["duration"] / 1000,
    )
    record = TargetRecord(
        repo=entry["repo"], sha=entry["sha"], harvested_at=provenance["fetched_at"],
        cells=(cell,), matched_cell=cell_id,
        notes=(
            "Frozen small CI benchmark. Counts include skipped final testcase rows, as in SAG-MS-1's current adapter; non-skipped counts are also reported separately.",
            "Full original testcase rows and byte hashes are archived; no count is inferred from a success badge.",
            entry.get("scope_note", "Full selected official Maven build/test invocation."),
        ),
    )
    return record, details


def download(candidates: Path, out: Path) -> None:
    if out.exists():
        raise FileExistsError(f"Refusing existing evidence directory: {out}")
    entries = json.loads(candidates.read_text())["projects"]
    out.mkdir(parents=True)
    for entry in entries:
        folder = out / entry["seat"]
        folder.mkdir()
        files = {}
        requests = [
            ("build.json", "api/json?depth=1"),
            ("console.log", "consoleText"),
            ("test-report.json", "testReport/api/json?depth=2"),
        ]
        if entry.get("shell_node"):
            requests += [
                ("stage.json", f"execution/node/{entry['stage_node']}/wfapi/describe"),
                ("node-console.html", f"execution/node/{entry['shell_node']}/log/"),
            ]
        for name, suffix in requests:
            url = entry["build_url"] + suffix
            raw = urlopen(url, timeout=60).read()
            (folder / name).write_bytes(raw)
            files[name] = {"url": url, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        url = f"https://raw.githubusercontent.com/{entry['repo']}/{entry['sha']}/pom.xml"
        raw = urlopen(url, timeout=60).read()
        (folder / "source-pom.xml").write_bytes(raw)
        files["source-pom.xml"] = {"url": url, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        runner.save(folder / "provenance.json", {
            "build_url": entry["build_url"], "fetched_at": runner.now(), "files": files,
        })
        print(json.dumps({"downloaded": entry["seat"]}), flush=True)


def freeze(candidates: Path, evidence: Path, out: Path) -> dict:
    if out.exists():
        raise FileExistsError(f"Refusing existing benchmark: {out}")
    proposal = json.loads(candidates.read_text())
    entries = proposal["projects"]
    if len(entries) != 10 or len({e["repo"] for e in entries}) != 10:
        raise ValueError("Benchmark must contain exactly 10 distinct repositories")
    checked = []
    for entry in entries:
        if entry.get("heavy") or not re.fullmatch(r"[0-9a-f]{40}", entry["sha"]):
            raise ValueError("Heavy or unpinned project in small benchmark")
        target, counts = read_official(entry, evidence / entry["seat"])
        if counts["reported"] > 10000 or counts["build_modules"] > 12:
            raise ValueError("Candidate exceeds the predeclared small-bench scope bounds")
        checked.append((entry, target, counts))
    out.mkdir(parents=True)
    projects = []
    for entry, target, counts in checked:
        target_path = out / "targets" / f"{entry['seat']}.json"
        runner.save(target_path, target.model_dump(mode="json"))
        projects.append(entry | {
            "target_file": str(target_path.resolve()), "target_sha256": runner.digest(target_path),
            "evidence_dir": str((evidence / entry["seat"]).resolve()), "official": counts,
        })
    manifest = {
        "benchmark": out.name, "frozen_at": runner.now(), "projects": projects,
        "selection": proposal["selection"], "excluded": proposal["excluded"],
        "measurement": {
            "standard": "SAG-MS-1 Part IV, refined by 2026-09-07 CI-defined-build-scope owner clarification",
            "alpha_build": "matched successful SAG modules / official successful CI modules",
            "alpha_test": "min(SAG final reported testcase rows, CI final reported testcase rows) / CI final reported testcase rows",
            "alpha": "min(alpha_build, alpha_test), only from the production authority-gated attainment result",
            "missing": "missing scope/authority is unscored, not success and not synthetic 1/1",
            "raw_counts": "CI and SAG reported, passed, red, skipped and non-skipped counts are shown separately",
            "lifecycle": "separate parity field; publication-only deploy/site is outside the build/test goal where explicitly listed",
            "headlines": "met or exceeded / all 10 frozen tasks; scorable / 10; completed build/test / 10",
            "comparison": "new CI-anchored task baseline, not a paired improvement claim against R11's different 23 tasks",
        },
        "collector_sha256": runner.digest(Path(__file__)),
    }
    runner.save(out / "manifest.json", manifest)
    (out / "manifest.sha256").write_text(runner.digest(out / "manifest.json") + "\n")
    lines = ["# Small CI benchmark — 10 projects", "", f"Frozen {manifest['frozen_at']}.", "",
             "Every row has a completed official CI build, exact source SHA and complete JUnit testcase pool.", "",
             "| Project | SHA | CI modules | Test records | Passed | Red | Skipped | JDK | Maven | Official CI |",
             "|---|---|---:|---:|---:|---:|---:|---|---|---|"]
    for p in projects:
        c = p["official"]
        lines.append(f"| {p['seat']} | `{p['sha'][:12]}` | {c['build_modules']} | {c['reported']} | {c['passed']} | {c['red']} | {c['skipped']} | {p['jdk_major']} | {p['maven_version']} | [run]({p['build_url']}) |")
    lines += ["", "Counts are final JUnit rows, including skips. Actual executions exclude skipped rows.",
              "SAG scores must come from the existing production evaluator; missing authority remains unscored.", ""]
    (out / "README.md").write_text("\n".join(lines))
    return manifest


def project_goal(project: dict) -> str:
    """Keep command bytes separate from prose punctuation."""
    return (
        f"At the pinned commit {project['sha']} of {project['repo']}, reproduce the official CI build and test scope.\n"
        f"Use Linux, JDK {project['jdk_major']} and Apache Maven {project['maven_version']}; verify and record the actual tool versions.\n"
        f"Official CI reference: {project['build_url']}\n\n"
        f"Official command (reference):\n```sh\n{project['official_command']}\n```\n\n"
        "The approved command for this build/test benchmark, from the repository root, is:\n"
        f"```sh\n{project['local_command']}\n```\n\n"
        f"{project['scope_note']} Preserve every selected module, test, profile and command flag. "
        "Inspect the pinned source, create and execute the production build/test plan, and retain its final reports. "
        "Report any unmet prerequisite or unfinished test scope explicitly."
    )


def project_task(project: dict):
    """Bind the measured CI tool version in new campaigns, beyond goal prose."""
    from sag.agent.acceptance_task import AcceptanceTask

    return AcceptanceTask.model_validate({
        "repo": project["repo"], "sha": project["sha"],
        "steps": [{
            "id": "ci-build-test", "runner": "maven", "cwd": ".",
            "argv": shlex.split(project["local_command"]),
            "java_major": int(project["jdk_major"]),
            "maven_version": project["maven_version"],
        }],
    })


def checked_benchmark_projects(benchmark: Path, bench: dict) -> list[tuple[dict, TargetRecord]]:
    """Revalidate the portable evidence bundle, not the collector's local paths."""
    archive = benchmark / "official-ci-evidence.tar.gz"
    if runner.digest(archive) != (benchmark / "official-ci-evidence.sha256").read_text().split()[0]:
        raise ValueError("Official evidence archive changed")
    checked = []
    with tempfile.TemporaryDirectory(prefix="small-ci-evidence-") as folder:
        with tarfile.open(archive) as tar:
            tar.extractall(folder, filter="data")
        for project in bench["projects"]:
            target = benchmark / "targets" / (project["seat"] + ".json")
            if runner.digest(target) != project["target_sha256"]:
                raise ValueError("Frozen target changed")
            record, counts = read_official(project, Path(folder) / project["seat"])
            if record.model_dump(mode="json") != json.loads(target.read_text()) or counts != project["official"]:
                raise ValueError("Frozen target differs from its official source evidence")
            checked.append((project | {"target_file": str(target.resolve())}, record))
    return checked


def prepare(benchmark: Path, out: Path, source: Path, sha: str, image: str, only: list[str] | None = None) -> dict:
    """Pin the existing SAG binary/configuration and all ten goals before running."""
    out = runner.safe_output(out)
    if out.exists():
        raise FileExistsError(f"Refusing existing campaign: {out}")
    bench_file = benchmark / "manifest.json"
    if runner.digest(bench_file) != (benchmark / "manifest.sha256").read_text().strip():
        raise ValueError("Benchmark manifest changed after freeze")
    bench = json.loads(bench_file.read_text())
    if len(bench["projects"]) != 10:
        raise ValueError("Prepared campaign must contain all ten frozen tasks")
    source = source.resolve()
    files = runner.verify_source(source, sha)
    image_info = json.loads(runner.command(["docker", "image", "inspect", "--format", "{{json .}}", image]))
    if image_info["Id"] != image:
        raise ValueError("Use an immutable local image ID")
    resources = json.loads(runner.command(["docker", "info", "--format", "{{json .}} "]))
    reference = benchmark / "runner-config.json"
    if runner.digest(reference) != reference.with_suffix(".sha256").read_text().strip():
        raise ValueError("Benchmark runner configuration changed")
    config = dict(json.loads(reference.read_text()))
    config.update(docker_base_image=image, max_iterations=75, max_wall_clock_seconds=1200)
    projects = []
    for i, (p, record) in enumerate(checked_benchmark_projects(benchmark, bench)):
        if only is not None and p["seat"] not in only:
            continue
        target = Path(p["target_file"])
        container = f"sag-{out.name}-{p['seat']}"
        if runner.inspect_container(container) is not None:
            raise ValueError("Prepared container already exists")
        goal = project_goal(p)
        projects.append(p | dict(
            variant="candidate", run_key=p["seat"], order=i, container=container, goal=goal,
            matched_cell=record.matched_cell, target_source=str(target),
            acceptance_command=p["local_command"],
            target_file=str(out / "targets" / target.name),
        ))
    if only is not None and {p["seat"] for p in projects} != set(only):
        raise ValueError("Unknown or missing project in selected cohort")
    manifest = dict(
        cohort=[p["seat"] for p in projects],
        campaign=out.name, created_at=runner.now(), phase="small-ci-10",
        reference_manifest=str(reference), reference_sha256=runner.digest(reference),
        benchmark_manifest=str(bench_file.resolve()), benchmark_sha256=runner.digest(bench_file),
        sources={"candidate": {"path": str(source), "sha": sha, "files": files}},
        config=config, environment=runner.config_environment(config),
        docker_image_id=image,
        docker_resources={k: resources.get(k) for k in ("NCPU", "MemTotal", "Architecture", "OperatingSystem", "ServerVersion")},
        image={k: image_info.get(k) for k in ("Id", "Architecture", "Os", "RepoDigests")},
        cache_policy="One fresh container per project, no host dependency/project cache; official CI cache differs and time is descriptive only",
        schedule="Selected frozen tasks run serially; one attempt each; no retries or project replacements based on SAG outcomes",
        python_environment=str(ROOT / ".venv"), projects=projects,
        runner_path=str(Path(runner.__file__).resolve()), runner_sha256=runner.digest(Path(runner.__file__)),
        small_runner_sha256=runner.digest(Path(__file__)),
    )
    out.mkdir(parents=True)
    (out / "targets").mkdir()
    (out / "tasks").mkdir()
    for project in projects:
        shutil.copyfile(project["target_source"], project["target_file"])
        task_path = out / "tasks" / f"{project['seat']}.json"
        task_path.write_text(project_task(project).model_dump_json(indent=2) + "\n")
        project["acceptance_task_file"] = str(task_path)
        project["acceptance_task_sha256"] = runner.digest(task_path)
    runner.command(["git", "archive", "--format=tar.gz", f"--output={out / 'candidate-source.tar.gz'}", sha], cwd=source, timeout=120)
    runner.save(out / "manifest.json", manifest)
    (out / "manifest.sha256").write_text(runner.digest(out / "manifest.json") + "\n")
    return manifest


def run(out: Path, only: list[str] | None) -> None:
    manifest = runner.load_manifest(out)
    if manifest["small_runner_sha256"] != runner.digest(Path(__file__)):
        raise ValueError("Small campaign runner changed after preparation")
    if manifest["benchmark_sha256"] != runner.digest(Path(manifest["benchmark_manifest"])):
        raise ValueError("Benchmark changed after preparation")
    chosen = set(only or [p["run_key"] for p in manifest["projects"]])
    if not chosen <= {p["run_key"] for p in manifest["projects"]}:
        raise ValueError("Unknown run key")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: runner.STOP.set())
    for project in manifest["projects"]:
        if project["run_key"] not in chosen or runner.STOP.is_set():
            continue
        runner.run_one(out, manifest, project)
    runner.event(out, "selection_complete", run_keys=sorted(chosen))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "freeze", "prepare", "run"))
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--sha")
    parser.add_argument("--image")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "download":
        if args.candidates is None:
            parser.error("download requires --candidates")
        download(args.candidates, args.out)
    elif args.action == "freeze":
        if args.evidence is None or args.candidates is None:
            parser.error("freeze requires --candidates and --evidence")
        result = freeze(args.candidates, args.evidence, args.out)
        print(json.dumps({"benchmark": result["benchmark"], "admitted": len(result["projects"])}))
    elif args.action == "prepare":
        if not all((args.benchmark, args.source, args.sha, args.image)):
            parser.error("prepare requires --benchmark, --source, --sha, --image")
        result = prepare(args.benchmark, args.out, args.source, args.sha, args.image, only=args.only)
        print(json.dumps({"prepared": result["campaign"], "attempts": len(result["projects"])}))
    else:
        run(args.out.resolve(), args.only)


if __name__ == "__main__":
    main()
