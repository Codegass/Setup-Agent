"""Bind one complete Jenkins Maven job to its frozen command and Git revision.

This transport is for MavenModuleSetBuild, not pipeline/matrix fan-out. Unknown
layouts stay unavailable. It reads archived bytes only and never follows URLs.
"""

import json
import posixpath
import re
import shlex
from urllib.parse import unquote, urlsplit

from .ci_native import ci_native_text
from .ci_sources import read_source
from .requirements import validate_ci_count_metadata


def _repo(url):
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/").removesuffix(".git")
    if parsed.scheme != "https":
        return None
    if parsed.netloc == "github.com":
        return path.lstrip("/")
    if parsed.netloc == "gitbox.apache.org" and path.startswith("/repos/asf/"):
        name = path.removeprefix("/repos/asf/")
        return "apache/" + name if name and "/" not in name else None
    return None


def selected_jenkins(task, spec, base):
    alignment = spec["ci_alignment"]
    index_ref = alignment["archived_ci_index"]
    index = json.loads(read_source(base, index_ref))
    url = alignment["selected_url"].rstrip("/") + "/"
    parsed = urlsplit(url)
    number = re.search(r"/job/.+/(\d+)/$", parsed.path)
    if parsed.scheme != "https" or not number:
        raise ValueError("Unsupported selected Jenkins build URL")
    if (
        index.get("selected_url", "").rstrip("/") + "/" != url
        or index.get("selected_cell") != alignment["selected_cell"]
    ):
        raise ValueError("Jenkins index does not bind the selected build/cell")

    def source(suffix):
        entries = [
            r
            for r in index.get("evidence", [])
            if (
                urlsplit(r.get("official_url") or "").scheme,
                urlsplit(r.get("official_url") or "").netloc,
                urlsplit(r.get("official_url") or "").path,
            )
            == (parsed.scheme, parsed.netloc, parsed.path + suffix)
        ]
        if len(entries) != 1:
            raise ValueError("Jenkins index needs one independently bound " + suffix)
        return entries[0]

    build_ref, console_ref = source("api/json"), source("consoleText")
    build = json.loads(read_source(base, build_ref))
    if (
        build.get("_class") != "hudson.maven.MavenModuleSetBuild"
        or build.get("number") != int(number[1])
        or build.get("url", "").rstrip("/") + "/" != url
        or build.get("building") is not False
        or build.get("result") != "SUCCESS"
    ):
        raise ValueError("Jenkins source is not the selected completed successful Maven job")
    git = [
        a
        for a in build.get("actions", [])
        if a.get("_class") == "hudson.plugins.git.util.BuildData"
    ]
    if (
        len(git) != 1
        or git[0].get("lastBuiltRevision", {}).get("SHA1") != task["sha"]
        or {_repo(r) for r in git[0].get("remoteUrls", [])} != {task["repo"]}
    ):
        raise ValueError("Jenkins SCM record differs from the pinned repository/commit")
    raw = read_source(base, console_ref)
    text = ci_native_text(raw.decode("utf-8"))
    if (
        not text.rstrip().endswith("Finished: SUCCESS")
        or re.findall(r"^Finished: (.+)$", text, re.M) != ["SUCCESS"]
        or re.findall(r"^\[INFO\]\s+BUILD (SUCCESS|FAILURE)\s*$", text, re.M) != ["SUCCESS"]
        or re.search(r"Using the MultiThreadedBuilder|^\[Pipeline\]", text, re.M)
    ):
        raise ValueError(
            "Jenkins console is incomplete, repeated or unsupported parallel/pipeline output"
        )
    if set(re.findall(r"^Checking out Revision ([0-9a-f]{40})\b", text, re.M)) != {task["sha"]}:
        raise ValueError("Jenkins console checkout does not bind the pinned commit")
    checkouts = re.findall(r"^ > git checkout -f ([0-9a-f]{40})\b", text, re.M)
    if checkouts != [task["sha"]]:
        raise ValueError("Jenkins command checkout is missing or ambiguous")
    roots = re.findall(r"^Building .* in workspace (/.+)\s*$", text, re.M)
    commands = re.findall(r"^Executing Maven:\s*(.+)$", text, re.M)
    if len(roots) != 1 or len(commands) != 1 or len(task["steps"]) != 1:
        raise ValueError("Jenkins requires one complete root task and one native Maven invocation")
    step = task["steps"][0]
    if step["runner"] != "maven" or step.get("cwd", ".") != "." or step["argv"][0] != "mvn":
        raise ValueError(
            "Jenkins Maven transport does not certify this launcher or working directory"
        )
    original = shlex.split(index.get("original_command", ""))
    if original != step["argv"]:
        raise ValueError("Jenkins original command differs from the frozen task")
    observed = ["mvn", *shlex.split(commands[0])]
    expected = list(step["argv"])
    # Jenkins's Maven plugin prints an absolute -f path. Bind that expansion to
    # the observed workspace; do not broadly strip paths or normalize options.
    for i, arg in enumerate(expected[:-1]):
        if arg in {"-f", "--file"}:
            relative = expected[i + 1]
            if posixpath.isabs(relative) or ".." in relative.split("/"):
                raise ValueError("Jenkins frozen POM path is outside the root task")
            expected[i + 1] = posixpath.normpath(posixpath.join(roots[0], relative))
    if observed != expected or any(a.startswith(("-T", "--threads")) for a in observed):
        raise ValueError("Jenkins native argv or POM location differs from the frozen task")
    native = text[text.index("Executing Maven:") :]
    java = re.findall(r"^Java version:\s*([\d.]+)", native, re.M)
    maven = re.findall(r"^Apache Maven\s+(\d+\.\d+\.\d+)", native, re.M)
    if len(java) != 1 or len(maven) != 1:
        raise ValueError("Jenkins actual Maven launcher runtime is not unique")
    major = int(java[0].split(".")[1 if java[0].startswith("1.") else 0])
    if any(
        expected is not None and expected != actual
        for expected, actual in (
            (step.get("java_major"), major),
            (step.get("maven_version"), maven[0]),
        )
    ):
        raise ValueError("Jenkins observed runtime differs from the frozen task")
    semantics = validate_ci_count_metadata(spec, base=base, required=True)
    if semantics["status"] != "available":
        raise ValueError("Jenkins native testReport count semantics unavailable")
    report = json.loads(read_source(base, semantics["sources"]["test_report"]))
    if report.get("_class") == "hudson.maven.reporters.SurefireAggregatedReport":
        children = report.get("childReports")
        if not isinstance(children, list) or not children:
            raise ValueError("Jenkins aggregate lacks child build identities")
        parent = parsed.path.rstrip("/").rsplit("/", 1)[0] + "/"
        for child in children:
            native_child = child.get("child") or {}
            child_url = urlsplit(native_child.get("url") or "")
            suffix = child_url.path.removeprefix(parent)
            if (
                native_child.get("_class") != "hudson.maven.MavenBuild"
                or native_child.get("number") != build["number"]
                or (child_url.scheme, child_url.netloc) != (parsed.scheme, parsed.netloc)
                or not child_url.path.startswith(parent)
                or not re.fullmatch(r"[^/]+/" + str(build["number"]) + r"/", suffix)
            ):
                raise ValueError("Jenkins testReport child belongs to another build")
            artifact = unquote(suffix.split("/")[0]).rsplit("$", 1)[-1]
            modules = {r["module"] for r in spec["requirements"] if r["kind"] == "test"}
            if artifact not in modules:
                raise ValueError("Jenkins report child has no frozen test module")
    # Check independent build API counts as well as the testReport API itself.
    reports = [
        a
        for a in build.get("actions", [])
        if a.get("_class")
        in {
            "hudson.maven.reporters.SurefireAggregatedReport",
            "hudson.tasks.junit.TestResultAction",
        }
    ]
    if len(reports) != 1 or any(
        reports[0].get(k) != semantics[v]
        for k, v in (
            ("totalCount", "reported_count"),
            ("failCount", "failed_or_error_count"),
            ("skipCount", "skipped_count"),
        )
    ):
        raise ValueError("Jenkins build API and native testReport disagree")
    sources = {
        "index": index_ref,
        "build": build_ref,
        "job_log": console_ref,
        "test_report": semantics["sources"]["test_report"],
    }
    identity = {
        "transport": "jenkins-maven-job-v1",
        "build_url": url,
        "build_number": build["number"],
        "selected_cell": alignment["selected_cell"],
        "repo": task["repo"],
        "sha": task["sha"],
        "workspace": roots[0],
        "native_argv": observed,
    }
    runtime = {
        "java_major": major,
        "maven_version": maven[0],
        "basis": "native_maven_version_banner",
    }
    return raw, sources, identity, semantics, {step["id"]: runtime}
