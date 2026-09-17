"""Main CLI interface for SAG (Setup-Agent)."""

import builtins
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import click
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sag import __version__
from sag.agent.agent import SetupAgent
from sag.agent.context_journal import JOURNAL_DIR
from sag.agent.history_state import HistoryActionState, decode_history_action_state
from sag.agent.phase_machine import PHASE_NAMES
from sag.agent.verdict_finalizer import (
    RunTermination,
    RunVerdictSnapshot,
    read_live_verdict_snapshot,
)
from sag.config import (
    Config,
    LogLevel,
    ensure_session_logging,
    get_config,
    get_session_logger,
    set_config,
)
from sag.console.result_block import render_result_block
from sag.console.turn_stream import TurnStreamRenderer
from sag.coverage.runner import apply_coverage
from sag.docker_orch.orch import DockerOrchestrator
from sag.result_card import build_result_card
from sag.runtime.container_io import read_container_text
from sag.tools.module_metrics import MODULE_METRICS_PATH
from sag.trajectory.builder import build_trajectory, control_events_path, follow_trajectory
from sag.trajectory.schema import DETAIL_TIERS
from sag.utils.git_utils import extract_project_name_from_url
from sag.web.server import run_web_server

console = Console()

# Note: You may see "Exception ignored while finalizing... ValueError: I/O operation on closed file"
# at the end of execution. This is a harmless cleanup issue from urllib3/docker-py during
# garbage collection and does not affect functionality. Python already handles it gracefully.


def _render_setup_cli_result(
    snapshot: RunVerdictSnapshot,
    termination: RunTermination,
    project_name: str,
    *,
    module_metrics: Mapping[str, Any] | None = None,
    report_metrics: Mapping[str, Any] | None = None,
    run_pin: Mapping[str, Any] | None = None,
    token_usage: Any = None,
    trajectory_session: Mapping[str, Any] | None = None,
    turn_count: int | None = None,
    tool_calls: int | None = None,
    tool_failures: int | None = None,
    container: str | None = None,
    session_dir: str | None = None,
    report_path: str | None = None,
) -> tuple[str, int]:
    """Render the run's result block and translate only its verdict to an exit code."""

    card = build_result_card(
        snapshot,
        module_metrics=module_metrics,
        report_metrics=report_metrics,
        run_pin=run_pin,
        token_usage=token_usage,
        trajectory_session=trajectory_session,
        turn_count=turn_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        termination=termination,
        project=project_name,
        container=container,
        session_dir=session_dir,
        report_path=report_path,
    )
    return render_result_block(card, width=console.width), (
        0 if snapshot.verdict == "success" else 1
    )


def _read_module_metrics_for_cli(orchestrator: DockerOrchestrator) -> Mapping[str, Any] | None:
    """Read the per-module diagnostic file, or nothing when it is absent."""

    try:
        text = read_container_text(orchestrator, MODULE_METRICS_PATH)
    except Exception:
        return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _report_name_for_block(report_path: str | None, session_dir: str | None) -> str | None:
    """Name the report the way the block has room to print it.

    The Evidence line directly above names the session directory, so a report
    copied into it is named by its file rather than by a path several times
    wider than the column, which would break the row it is printed in.
    """

    if not report_path or not session_dir:
        return report_path
    try:
        return str(Path(report_path).relative_to(session_dir))
    except ValueError:
        return report_path


def _read_run_pin_for_cli(session_logger: Any) -> Mapping[str, Any] | None:
    """Read the host's run pin, or nothing when this run did not write one."""

    if session_logger is None:
        return None
    try:
        parsed = json.loads(Path(session_logger.run_pin_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_run_counts_for_cli(session_dir: str | None) -> dict[str, Any]:
    """Fold the session's own ledger into the counts the result block states.

    Every count is absent unless the ledger supplied it: a run whose turns were
    never recorded reports no turns, not zero turns, and a session directory
    that cannot be read leaves the whole group absent rather than raising.
    """

    counts: dict[str, Any] = {
        "trajectory_session": None,
        "turn_count": None,
        "tool_calls": None,
        "tool_failures": None,
        "token_usage": None,
    }
    if not session_dir:
        return counts
    try:
        document = build_trajectory(session_dir)
    except Exception as exc:
        logger.debug(f"run counts for the result block are unavailable: {exc}")
        return counts

    counts["trajectory_session"] = document.session.model_dump(mode="json")
    # `list` is this module's `sag list` command, not the builtin.
    turns = tuple(document.turns)
    if not turns:
        return counts
    counts["turn_count"] = len(turns)
    counts["tool_calls"] = sum(1 for turn in turns if turn.call is not None)
    counts["tool_failures"] = sum(
        1
        for turn in turns
        if turn.observation is not None
        and (turn.observation.error_code or turn.observation.failure_signature)
    )
    # The bill each turn carries, not the token file's raw rows: the derivation
    # has already decided which row pays for which turn, and a second reader
    # adding the rows up itself would double-count the duplicates it dropped.
    billed = tuple(
        {"prompt_tokens": turn.tokens.input, "output_tokens": turn.tokens.output}
        for turn in turns
        if turn.tokens is not None
    )
    counts["token_usage"] = billed or None
    return counts


def _read_metrics_v2_for_cli(orchestrator: DockerOrchestrator) -> Mapping[str, Any] | None:
    """Read only host-authorized current metrics; legacy stays forensic."""

    from sag.tools.report_metrics import read_live_report_metrics

    try:
        read = read_live_report_metrics(orchestrator)
    except (AttributeError, TypeError, ValueError):
        return None
    return read.payload if read.complete and read.conflict is None else None


def _start_agent_session_logging(config: Config) -> None:
    """Initialize session logging at the point a command starts agent work."""
    session_logger = ensure_session_logging(config)
    if config.verbose and session_logger:
        logger.info(f"Session ID: {session_logger.session_id}")
        logger.info(f"Logs directory: {session_logger.session_log_dir}")


def _turn_stream_sink() -> Callable[[str], None]:
    """Where a turn stream writes. One function, because there are two callers.

    It writes through `console.print`, not `console.file.write`: the latter is
    a raw stream write that interprets no markup, so on a real terminal every
    styled token would arrive as a literal `[red]failed[/red]`. All three
    keywords are load bearing — `end=""` because the renderer owns its newlines
    and completes a dispatch line in place, `soft_wrap=True` because Rich would
    otherwise re-wrap a line the renderer has already fitted to the width, and
    `highlight=False` because Rich's auto-highlighter would colour numbers
    inside a summary the renderer is already styling.

    The live run and `sag trajectory` share it so that a fix here cannot land
    at one wiring site and miss the other, which is how the raw write survived
    its first correction.
    """

    return lambda text: console.print(text, end="", markup=True, highlight=False, soft_wrap=True)


def _attach_turn_stream() -> Optional[TurnStreamRenderer]:
    """Show the run as turns. Returns the renderer so the caller can close it.

    The renderer stands beside the session's control ledger and reads the same
    lines the ledger keeps — never a log line.
    """

    session_logger = get_session_logger()
    if session_logger is None:
        return None
    renderer = TurnStreamRenderer(
        _turn_stream_sink(),
        width=console.width,
        tty=console.is_terminal,
    )
    session_logger.get_control_event_sink().add_observer(renderer.feed)
    return renderer


def _close_turn_stream(renderer: Optional[TurnStreamRenderer]) -> None:
    """End the stream: stop watching, then flush. Never raises.

    Both halves matter. The renderer comes off the sink first because the sink
    outlives it and a finished renderer refuses to be fed — that refusal would
    travel through stdlib `logging`, which nothing here routes, and land on the
    console this layer just quieted. And nothing in here may raise: this runs
    from the `finally` of a command that ends in a broad `except`, so a write
    that fails while closing — a closed terminal, a `BrokenPipeError` from
    `sag project … | head` — would otherwise replace whatever the run actually
    died of with a rendering error. The run's own exception outranks any
    renderer, exactly as the ledger's append outranks any observer.
    """

    if renderer is None:
        return
    session_logger = get_session_logger()
    if session_logger is not None:
        try:
            session_logger.get_control_event_sink().remove_observer(renderer.feed)
        except Exception as exc:
            logger.warning(f"Could not detach the turn stream from the control stream: {exc}")
    try:
        renderer.close()
    except Exception as exc:
        logger.warning(f"The turn stream failed to finish its last line: {exc}")


def _execute_control(orchestrator, command: str, **kwargs):
    """Run mechanical container I/O without requiring a project runtime."""

    execute = getattr(orchestrator, "execute_control_command", None)
    if not callable(execute):
        execute = orchestrator.execute_command
    return execute(command, **kwargs)


def detect_project_directory_in_container(orchestrator: DockerOrchestrator) -> Optional[str]:
    """
    Detect the actual project directory inside a running container.

    This is needed when --name was used to create a container with a different
    name than the git repository. For example:
    - Container: sag-CommonsCli
    - Actual directory: /workspace/commons-cli

    Args:
        orchestrator: Docker orchestrator for the container

    Returns:
        The detected project name (directory name under /workspace), or None if not found
    """
    try:
        # List directories in /workspace, excluding system directories
        result = _execute_control(orchestrator, "ls -d /workspace/*/ 2>/dev/null | head -10")

        if result.get("exit_code") != 0:
            return None

        dirs = [d.strip().rstrip("/") for d in result.get("output", "").split("\n") if d.strip()]

        # Filter out system directories
        project_dirs = [
            d
            for d in dirs
            if d.startswith("/workspace/") and ".setup_agent" not in d and "setup-report-" not in d
        ]

        if not project_dirs:
            return None

        # Check for directories with build files (pom.xml, build.gradle, package.json, etc.)
        for dir_path in project_dirs:
            check_result = _execute_control(
                orchestrator,
                f"test -f {dir_path}/pom.xml || test -f {dir_path}/build.gradle || "
                f"test -f {dir_path}/package.json || test -f {dir_path}/requirements.txt || "
                f"test -f {dir_path}/pyproject.toml && echo FOUND || echo NOTFOUND",
            )
            if "FOUND" in check_result.get("output", ""):
                # Return just the directory name, not full path
                return dir_path.split("/")[-1]

        # If no build file found, return the first visible directory name
        visible_dirs = [d for d in project_dirs if not d.split("/")[-1].startswith(".")]
        if visible_dirs:
            return visible_dirs[0].split("/")[-1]

        return None

    except Exception as e:
        logger.warning(f"Failed to detect project directory: {e}")
        return None


def read_project_metadata(orchestrator: DockerOrchestrator) -> Optional[Dict[str, Any]]:
    """Forensically read the legacy container project metadata mirror.

    This metadata is created during `sag project` and contains:
    - project_name: The actual project directory name (from URL)
    - project_url: The Git repository URL
    - docker_label: The Docker container label (from --name or project_name)
    - goal: The setup goal description
    - created_at: When the project was set up

    Args:
        orchestrator: Docker orchestrator for the container

    Returns:
        Dictionary with project metadata, or None if not found/readable
    """
    try:
        result = _execute_control(
            orchestrator, "cat /workspace/.setup_agent/project_meta.json 2>/dev/null"
        )

        if result.get("exit_code") != 0:
            logger.debug("project_meta.json not found or not readable")
            return None

        output = result.get("output", "").strip()
        if not output:
            return None

        metadata = json.loads(output)
        logger.info(f"✅ Read project metadata: project_name={metadata.get('project_name')}")
        return metadata

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse project_meta.json: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to read project metadata: {e}")
        return None


def _save_setup_artifacts(orchestrator: DockerOrchestrator, project_name: str) -> str | None:
    """Copy setup artifacts from Docker container to local session logs.

    Returns the host path of the setup report that was copied, so the result
    block can name the file a reader can actually open. ``None`` whenever no
    report reached the host, for any reason; copying is best-effort and never
    fails the run.

    Args:
        orchestrator: Docker orchestrator for the project
        project_name: Name of the project
    """
    copied_report: str | None = None
    try:
        session_logger = get_session_logger()
        if not session_logger:
            logger.warning("No session logger available, skipping artifact save")
            return None

        # Get the session log directory
        session_dir = session_logger.session_log_dir
        if not session_dir.exists():
            session_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving artifacts to {session_dir}")

        # Check if .setup_agent folder exists in container
        check_result = _execute_control(
            orchestrator, "test -d /workspace/.setup_agent && echo 'EXISTS' || echo 'NOT_FOUND'"
        )

        if check_result.get("output", "").strip() == "EXISTS":
            # Copy .setup_agent folder
            copy_cmd = (
                f"docker cp {orchestrator.container_name}:/workspace/.setup_agent {session_dir}/"
            )
            # `subprocess` is imported at module scope. A second import here
            # made the name local to this whole function, so a container with
            # no `.setup_agent` folder left the report copy below reading an
            # unbound local and losing every report it was asked to save.
            result = subprocess.run(copy_cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("✅ Copied .setup_agent folder from container")
            else:
                logger.warning(f"Failed to copy .setup_agent folder: {result.stderr}")
        else:
            logger.info(".setup_agent folder not found in container, skipping")

        # Find and copy setup-report-*.md files
        find_result = _execute_control(
            orchestrator,
            "find /workspace -maxdepth 1 -name 'setup-report-*.md' -type f 2>/dev/null | head -10",
        )

        report_files = find_result.get("output", "").strip().split("\n")
        report_files = [f for f in report_files if f.strip()]

        if report_files:
            for report_file in report_files:
                if report_file:
                    # Extract filename from full path
                    filename = report_file.split("/")[-1]
                    copy_cmd = f"docker cp {orchestrator.container_name}:{report_file} {session_dir}/{filename}"
                    result = subprocess.run(copy_cmd, shell=True, capture_output=True, text=True)
                    if result.returncode == 0:
                        logger.info(f"✅ Copied {filename} from container")
                        copied_report = str(session_dir / filename)
                    else:
                        logger.warning(f"Failed to copy {filename}: {result.stderr}")
        else:
            logger.info("No setup report files found in container")

        console.print(f"[dim]Artifacts saved to: {session_dir}[/dim]")

    except Exception as e:
        logger.error(f"Failed to save artifacts: {e}")
        # Don't fail the main operation if artifact saving fails
        console.print(f"[yellow]⚠️ Could not save artifacts: {e}[/yellow]")
    return copied_report


def _detect_coverage_build_system(orchestrator, project_dir: str):
    """Detect maven/gradle physically for the coverage pass (or None)."""
    try:
        from sag.agent.physical_validator import PhysicalValidator

        bs = PhysicalValidator(docker_orchestrator=orchestrator)._detect_build_system(project_dir)
        return bs if bs in ("maven", "gradle") else None
    except Exception as exc:
        logger.debug(f"coverage build-system detect failed: {exc}")
        return None


def _coverage_baseline_metrics(validator, project_name: str) -> dict[str, Any] | None:
    """Project the gate's cached module scan onto the metrics artifact shape."""

    if validator is None:
        return None
    try:
        scan = validator.module_scan(project_name)
    except Exception as exc:
        logger.debug(f"coverage baseline module scan unavailable: {exc}")
        return None
    if not isinstance(scan, Mapping):
        return None
    summary = scan.get("summary")
    modules = scan.get("modules")
    if not isinstance(summary, Mapping) or not isinstance(modules, builtins.list) or not modules:
        return None
    return {
        "version": 1,
        "generated_at": "coverage-prefinalize",
        "module_summary": dict(summary),
        "modules": [dict(module) for module in modules if isinstance(module, Mapping)],
    }


def _run_coverage_pass(orchestrator, project_name: str, *, validator=None) -> bool:
    """Run one isolated, best-effort coverage pass.

    Never raises; never changes the setup result. The entire body is guarded so
    that even an unexpected error here cannot reach the command's outer handler
    (which would sys.exit(1) and fail an already-successful setup). Warns if the
    project source tree changed (pollution guard)."""
    try:
        project_dir = f"/workspace/{project_name}"
        build_system = _detect_coverage_build_system(orchestrator, project_dir)
        if build_system is None:
            logger.info("Coverage: no maven/gradle build detected; skipping.")
            return False
        baseline = _coverage_baseline_metrics(validator, project_name)
        if baseline is None:
            wrote = apply_coverage(orchestrator, project_dir, build_system)
        else:
            wrote = apply_coverage(
                orchestrator,
                project_dir,
                build_system,
                baseline_metrics=baseline,
            )
        # Pollution guard (warn-only): tracked source files must be unchanged.
        dirty = _execute_control(
            orchestrator,
            f"cd {project_dir} && git status --porcelain 2>/dev/null "
            f"| grep -vE 'target/|build/|\\.setup_agent' | head -5",
        )
        if (dirty.get("output") or "").strip():
            logger.warning(f"Coverage pass left source-tree changes:\n{dirty['output']}")
        return wrote
    except Exception as exc:  # never propagate into the command's success/exit path
        logger.warning(f"Coverage pass failed (best-effort, ignored): {exc}")
        return False


def _run_coverage_evidence_pass(
    orchestrator,
    project_name: str,
    *,
    validator=None,
) -> dict[str, Any]:
    """Return the persisted coverage rollup without recomputing its math."""

    if not _run_coverage_pass(orchestrator, project_name, validator=validator):
        return {
            "status": "unavailable",
            "reason": "coverage pass produced no persisted reports",
        }
    try:
        raw = read_container_text(orchestrator, MODULE_METRICS_PATH, exact_bytes=True)
        payload = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
        summary = payload.get("module_summary") if isinstance(payload, dict) else None
        line_rate = summary.get("line_rate") if isinstance(summary, dict) else None
        source = summary.get("coverage_source") if isinstance(summary, dict) else None
        if (
            type(line_rate) not in (int, float)
            or not 0.0 <= float(line_rate) <= 100.0
            or not isinstance(source, str)
            or not source.strip()
        ):
            raise ValueError("persisted module coverage summary is incomplete")
        return {
            "status": "collected",
            "line_rate": round(float(line_rate), 1),
            "source": source.strip(),
        }
    except Exception as exc:
        logger.warning(f"Coverage summary unavailable after pass: {exc}")
        return {
            "status": "unavailable",
            "reason": "persisted coverage summary unavailable",
        }


@click.group()
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    help="Set the logging level",
)
@click.option("--log-file", type=click.Path(), help="Path to log file")
@click.option("--verbose", is_flag=True, help="Enable verbose debugging output with detailed logs")
@click.pass_context
def cli(ctx, log_level, log_file, verbose):
    """SAG: Setup-Agent - LLM Powered project setup automation."""

    # Create configuration
    config = Config.from_env()

    # Override with CLI options if provided
    if log_level:
        config.log_level = LogLevel(log_level)
    if log_file:
        config.log_file = log_file
    if verbose:
        config.verbose = verbose

    # Set global config without opening a session log. Session logs are for
    # agent executions only; read-only CLI commands should not create
    # logs/session_* directories.
    set_config(config, initialize_logging=False)

    # Ensure context object exists
    ctx.ensure_object(dict)
    ctx.obj["config"] = config

    # Display welcome message for main commands.
    # `trajectory` and `result` are excluded because their stdout is something
    # somebody parses — `sag result X --json | jq` is dead with a panel above it.
    if ctx.invoked_subcommand not in ["list", "trajectory", "result"] and not config.verbose:
        console.print(
            Panel.fit(
                "[bold blue]SAG[/bold blue] - [dim]Setup Agent[/dim]\n"
                "[dim]Automated project setup with AI[/dim]",
                border_style="blue",
            )
        )


@cli.command()
def list():
    """List all SAG-managed Docker containers with their status and last comment."""

    try:
        orchestrator = DockerOrchestrator()
        projects = orchestrator.list_sag_projects()

        if not projects:
            console.print("[yellow]No SAG projects found.[/yellow]")
            console.print("[dim]Use 'sag project <repo_url>' to create a new project.[/dim]")
            return

        # Create table
        table = Table(title="SAG Projects", show_header=True, header_style="bold magenta")
        table.add_column("Project Name", style="cyan", no_wrap=True)
        table.add_column("Docker Name", style="blue", no_wrap=True)
        table.add_column("Status", style="green")
        table.add_column("Last Comment", style="white", max_width=50)
        table.add_column("Created", style="dim")

        for project in projects:
            # Get status with color
            status = project["status"]
            if status == "running":
                status_text = Text("🟢 running", style="green")
            elif status == "exited":
                status_text = Text("🔴 stopped", style="red")
            else:
                status_text = Text(f"🟡 {status}", style="yellow")

            # Get last comment from agent
            last_comment = project.get("last_comment", "No comment available")
            # Show full comment without truncation

            table.add_row(
                project["project_name"],
                project["docker_name"],
                status_text,
                last_comment,
                project["created"],
            )

        console.print(table)
        console.print(
            f"\n[dim]Use 'sag run <docker_name> --task \"description\"' to continue working on a project.[/dim]"
        )

    except Exception as e:
        logger.error(f"List projects failed: {e}")
        console.print(f"[bold red]❌ Failed to list projects: {e}[/bold red]")


@cli.command()
@click.argument("repo_url")
@click.option(
    "--name",
    help="Override Docker container name (default: extracted from URL). Does not affect the cloned directory name.",
)
@click.option("--goal", help="Custom setup goal (default: auto-generated)")
@click.option(
    "--record", is_flag=True, help="Save setup artifacts (contexts, reports) to local session logs"
)
@click.option(
    "--coverage",
    is_flag=True,
    help="Run an isolated JaCoCo coverage pass before verdict close (best-effort)",
)
@click.option(
    "--ref",
    "project_ref",
    help="Git ref to set up, such as a branch, tag, release tag, short commit, or full commit.",
)
@click.option(
    "--ci-target-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Frozen official-CI target JSON to compare after execution; never fetched at finalization.",
)
@click.option(
    "--acceptance-command",
    help="Fixed build/test command from the repository root; binds receipts independently of the agent plan. Requires --ci-target-file.",
)
@click.option(
    "--acceptance-task-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Fixed ordered task JSON, independent of the agent plan and CI test-count availability.",
)
@click.pass_context
def project(
    ctx,
    repo_url,
    name,
    goal,
    record,
    coverage,
    project_ref,
    ci_target_file,
    acceptance_command,
    acceptance_task_file,
):
    """Initial setup for a new project from repository URL."""

    config = ctx.obj["config"]
    acceptance_task = None
    if acceptance_task_file is not None:
        from sag.agent.acceptance_task import load_acceptance_task
        from sag.agent.ci_comparison import repository_identity

        try:
            acceptance_task = load_acceptance_task(acceptance_task_file)
            if acceptance_task.repo != repository_identity(repo_url):
                raise ValueError("task repository differs from project URL")
            if project_ref is not None and project_ref != acceptance_task.sha:
                raise ValueError("--ref must equal the task's frozen commit SHA")
            project_ref = acceptance_task.sha
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Invalid acceptance task: {exc}") from exc
    ci_target = None
    if acceptance_command and ci_target_file is None:
        raise click.ClickException("--acceptance-command requires --ci-target-file")
    if ci_target_file is not None:
        from sag.agent.ci_comparison import load_ci_target

        try:
            if acceptance_command:
                from sag.tools.build.backends import parse_runner_command

                parse_runner_command(acceptance_command)
            ci_target = load_ci_target(ci_target_file, execution_command=acceptance_command)
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Invalid CI target: {exc}") from exc

    try:
        # ALWAYS extract project_name from URL - this is the actual directory name
        # The --name flag only affects Docker container/volume naming
        project_name = extract_project_name_from_url(repo_url)

        # docker_label is what the user provides via --name, or defaults to project_name
        docker_label = name if name else project_name

        # Generate default goal if not provided
        if not goal:
            goal = f"Setup and configure the {project_name} project to be runnable"

        docker_name = f"sag-{docker_label}"

        console.print(f"[bold green]🚀 Setting up new project[/bold green]")
        console.print(f"[dim]Repository:[/dim] {repo_url}")
        if project_ref:
            console.print(f"[dim]Repository Ref:[/dim] {project_ref}")
        console.print(f"[dim]Project Name:[/dim] {project_name}")
        console.print(f"[dim]Docker Name:[/dim] {docker_name}")
        if name and name != project_name:
            console.print(
                f"[dim]Note:[/dim] Using custom Docker name, project directory will be /workspace/{project_name}"
            )
        console.print(f"[dim]Goal:[/dim] {goal}")
        if record:
            console.print(f"[dim]Recording:[/dim] Enabled (artifacts will be saved locally)")

        # Check if project already exists (using docker_label for container naming)
        orchestrator = DockerOrchestrator(project_name=docker_label)
        if orchestrator.container_exists():
            console.print(
                f"[bold yellow]⚠️ Container '{docker_name}' already exists![/bold yellow]"
            )
            console.print(
                f"[dim]Use 'sag run {docker_name} --task \"description\"' to continue working on it.[/dim]"
            )
            return

        _start_agent_session_logging(config)
        turn_stream = _attach_turn_stream()

        # The close has to run from a `finally`: this command ends in a broad
        # `except Exception`, which would otherwise take the crash before the
        # stream flushed its last line and the warnings it is still holding —
        # on exactly the runs a reader most needs to read.
        try:
            # Initialize agent
            agent = SetupAgent(config=config, orchestrator=orchestrator)

            # Run the setup - pass project_name (from URL) and docker_label for metadata
            termination = agent.setup_project(
                project_url=repo_url,
                project_name=project_name,
                goal=goal,
                docker_label=docker_label,
                project_ref=project_ref,
                **({"ci_target": ci_target} if ci_target is not None else {}),
                **({"acceptance_task": acceptance_task} if acceptance_task is not None else {}),
                pre_finalize_evidence_callback=(
                    (
                        lambda: _run_coverage_evidence_pass(
                            orchestrator,
                            project_name,
                            validator=getattr(
                                getattr(agent, "react_engine", None),
                                "physical_validator",
                                None,
                            ),
                        )
                    )
                    if coverage
                    else None
                ),
            )
        finally:
            _close_turn_stream(turn_stream)

        snapshot = read_live_verdict_snapshot(orchestrator)
        session_logger = get_session_logger()
        session_dir = str(session_logger.session_log_dir) if session_logger else None

        # The block names the report a reader can open, so the copy has to have
        # happened before the block is built.
        report_path = _save_setup_artifacts(orchestrator, project_name) if record else None

        run_counts = _read_run_counts_for_cli(session_dir)
        cli_result, exit_code = _render_setup_cli_result(
            snapshot,
            termination,
            project_name,
            module_metrics=_read_module_metrics_for_cli(orchestrator),
            report_metrics=_read_metrics_v2_for_cli(orchestrator),
            run_pin=_read_run_pin_for_cli(session_logger),
            trajectory_session=run_counts["trajectory_session"],
            turn_count=run_counts["turn_count"],
            tool_calls=run_counts["tool_calls"],
            tool_failures=run_counts["tool_failures"],
            token_usage=run_counts["token_usage"],
            container=docker_name,
            session_dir=session_dir,
            report_path=_report_name_for_block(report_path, session_dir),
        )

        console.print(cli_result)

        if exit_code:
            sys.exit(exit_code)

    except Exception as e:
        logger.error(f"Project setup failed: {e}")
        console.print(f"[bold red]❌ Setup failed: {e}[/bold red]")
        sys.exit(1)


@cli.command()
@click.argument("docker_name")
@click.option("--task", required=True, help="Specific task or requirement for the agent")
@click.option("--max-iterations", default=None, type=int, help="Maximum number of agent iterations")
@click.option(
    "--record", is_flag=True, help="Save setup artifacts (contexts, reports) to local session logs"
)
@click.option(
    "--coverage",
    is_flag=True,
    help="Run an isolated JaCoCo coverage pass after setup (best-effort)",
)
@click.pass_context
def run(ctx, docker_name, task, max_iterations, record, coverage):
    """Run a specific task on an existing SAG project."""

    config = ctx.obj["config"]

    try:
        # Extract docker_label from docker name (this is the container identifier)
        if not docker_name.startswith("sag-"):
            console.print(f"[bold red]❌ Invalid docker name. Must start with 'sag-'[/bold red]")
            console.print(f"[dim]Use 'sag list' to see available projects.[/dim]")
            return

        docker_label = docker_name[4:]  # Remove 'sag-' prefix

        # Initialize orchestrator with docker_label (for container access)
        orchestrator = DockerOrchestrator(project_name=docker_label)

        # Check if container exists
        if not orchestrator.container_exists():
            console.print(f"[bold red]❌ Docker container '{docker_name}' not found![/bold red]")
            console.print(f"[dim]Use 'sag list' to see available projects.[/dim]")
            return

        # Ensure container is running before reading metadata
        if not orchestrator.is_container_running():
            console.print("[yellow]⚠️ Container is not running. Starting it...[/yellow]")
            orchestrator.start_container()

        # project_meta.json is project-writable forensic state, not routing
        # authority. Resolve the workspace mechanically and fall back to the
        # host-selected Docker label when no unique build root is observable.
        detected = detect_project_directory_in_container(orchestrator)
        if detected and detected != docker_label:
            actual_project_name = detected
            console.print(
                f"[dim]Note:[/dim] Detected project directory: /workspace/{actual_project_name}"
            )
        else:
            actual_project_name = detected or docker_label

        console.print(f"[bold green]🔧 Running task on project: {actual_project_name}[/bold green]")
        console.print(f"[dim]Docker:[/dim] {docker_name}")
        console.print(f"[dim]Task:[/dim] {task}")
        if record:
            console.print(f"[dim]Recording:[/dim] Enabled (artifacts will be saved locally)")

        _start_agent_session_logging(config)
        turn_stream = _attach_turn_stream()

        # Same reason as `project`: the stream's last line and its held
        # warnings belong to the reader even when the run dies.
        try:
            # Initialize agent
            final_max_iterations = (
                max_iterations if max_iterations is not None else config.max_iterations
            )
            agent = SetupAgent(
                config=config, orchestrator=orchestrator, max_iterations=final_max_iterations
            )

            # Run the task with the actual project name
            success = agent.run_task(project_name=actual_project_name, task_description=task)
        finally:
            _close_turn_stream(turn_stream)

        # Save artifacts if recording is enabled
        if record:
            _save_setup_artifacts(orchestrator, actual_project_name)

        if coverage:
            _run_coverage_pass(orchestrator, actual_project_name)

        if success:
            console.print(f"[bold green]✅ Task completed successfully![/bold green]")
        else:
            console.print(f"[bold yellow]⚠️ Task may be incomplete.[/bold yellow]")
            console.print(f"[dim]Check logs for details or run another task to continue.[/dim]")

    except Exception as e:
        logger.error(f"Task execution failed: {e}")
        console.print(f"[bold red]❌ Task failed: {e}[/bold red]")
        sys.exit(1)


@cli.command()
@click.argument("docker_name")
@click.option("--shell", default="/bin/bash", help="Shell to use in the container")
def shell(docker_name, shell):
    """Connect to a project's Docker container shell."""

    try:
        # Extract project name from docker name
        if not docker_name.startswith("sag-"):
            console.print(f"[bold red]❌ Invalid docker name. Must start with 'sag-'[/bold red]")
            return

        project_name = docker_name[4:]  # Remove 'sag-' prefix

        console.print(f"[bold green]🔗 Connecting to {docker_name}[/bold green]")

        orchestrator = DockerOrchestrator(project_name=project_name)

        if not orchestrator.container_exists():
            console.print(f"[bold red]❌ Container '{docker_name}' not found![/bold red]")
            return

        if not orchestrator.is_container_running():
            console.print("[yellow]Container is not running. Starting it...[/yellow]")
            orchestrator.start_container()

        console.print(f"[dim]Connecting with {shell}...[/dim]")
        orchestrator.connect_to_container(shell)

    except Exception as e:
        logger.error(f"Shell connection failed: {e}")
        console.print(f"[bold red]❌ Connection failed: {e}[/bold red]")


# --- sag inspect (spec §7): render context journals + phase history -------
#
# Pure render helpers first (unit-tested without click/docker); the command
# below only chooses a source (live container vs local --record artifact dir)
# and prints what they return.

_CONTEXTS_DIR_IN_CONTAINER = "/workspace/.setup_agent/contexts"


class _InspectError(Exception):
    """User-facing inspect failure: print the message and exit 1 (no traceback)."""


def _coerce_entry_list(value) -> List[Any]:
    """A JSON value → list of entries (anything non-array → []).

    NOTE: never `isinstance(x, list)` in this module — the module-level `list`
    click command shadows the builtin.
    """
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return [item for item in value]
    except TypeError:
        return []


def _inspect_sorted_records(records) -> List[Dict[str, Any]]:
    """Journal records ordered by iteration (defensive: bad records sort last)."""

    def key(rec):
        it = rec.get("iteration") if isinstance(rec, dict) else None
        return (0, it) if isinstance(it, int) else (1, 0)

    return sorted([r for r in records if isinstance(r, dict)], key=key)


_OUTPUT_REF_RE = re.compile(r"\boutput_[A-Za-z0-9_-]+\b")
_INSPECT_PHASE_RE = re.compile(r"[A-Za-z0-9_-]+")


def _inspect_validate_phase_name(phase: str) -> str:
    if not _INSPECT_PHASE_RE.fullmatch(phase or ""):
        raise _InspectError(f"Invalid phase name: {phase}")
    return phase


def _inspect_output_refs_from_text(value: str) -> List[str]:
    """Return output refs in first-seen order."""
    refs: List[str] = []
    seen = set()
    for ref in _OUTPUT_REF_RE.findall(value):
        if ref in seen:
            continue
        refs.append(ref)
        seen.add(ref)
    return refs


def _inspect_output_lookup(source) -> Callable[[str], Optional[str]]:
    return getattr(source, "full_output", lambda ref: None)


def _inspect_phase_task(source, phase: str) -> Dict[str, Any]:
    trunk = source.trunk_data()
    for task in (trunk or {}).get("todo_list", []) or []:
        if not isinstance(task, dict):
            continue
        if task.get("id") == f"phase_{phase}":
            return task
    return {}


def _inspect_clean_internal_output_markers(value: str) -> str:
    """Hide storage instructions while keeping the ref itself visible."""
    lines: List[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if re.match(r"^\.\.\. \[Output truncated:.*\] \.\.\.$", stripped, re.IGNORECASE):
            continue
        if re.match(r"^\.\.\. \[Search with:.*\] \.\.\.$", stripped, re.IGNORECASE):
            continue
        ref_match = re.match(
            r"^\.\.\. \[(?:Full output ref|FULL OUTPUT REF):\s*(output_[A-Za-z0-9_-]+)\] \.\.\.$",
            stripped,
            re.IGNORECASE,
        )
        if ref_match:
            lines.append(f"Full output ref: {ref_match.group(1)}")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _inspect_entry_full_outputs(
    entry: Dict[str, Any],
    output_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Tuple[str, str]]:
    if output_lookup is None:
        return []
    expanded: List[Tuple[str, str]] = []
    seen = set()
    for raw_value in (entry.get("output"), entry.get("content")):
        for ref in _inspect_output_refs_from_text(str(raw_value or "")):
            if ref in seen:
                continue
            seen.add(ref)
            content = output_lookup(ref)
            if content:
                expanded.append((ref, content))
    return expanded


def _inspect_format_block(label: str, value: str) -> List[str]:
    if not value:
        return []
    return [f"{label}:", textwrap.indent(value, "  ")]


def _inspect_format_history_entry(
    entry,
    *,
    index: Optional[int] = None,
    output_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Readable multi-line thought/action/history entry without extra truncation."""
    prefix = f"{index}. " if index is not None else ""
    if not isinstance(entry, dict):
        return f"{prefix}[?]\n{textwrap.indent(str(entry), '  ')}"

    etype = str(entry.get("type", "?"))
    lines: List[str] = []

    if etype == "action":
        history_state = decode_history_action_state(entry)
        status = {
            HistoryActionState.SUCCESS: "ok",
            HistoryActionState.FAILED: "failed",
            HistoryActionState.PENDING: "pending",
            HistoryActionState.UNKNOWN: "unknown",
        }[history_state]
        lines.append(f"{prefix}[action] {entry.get('tool_name', '?')} ({status})")
        parameters = entry.get("parameters")
        if parameters is None:
            parameters = entry.get("params")
        if parameters is not None:
            try:
                rendered_params = json.dumps(parameters, indent=2, sort_keys=True)
            except TypeError:
                rendered_params = str(parameters)
            lines.extend(_inspect_format_block("parameters", rendered_params))
        raw_output = _inspect_clean_internal_output_markers(str(entry.get("output") or ""))
        lines.extend(_inspect_format_block("output", raw_output))
        for ref, full_output in _inspect_entry_full_outputs(entry, output_lookup):
            lines.extend(_inspect_format_block(f"full output {ref}", full_output))
        return "\n".join(lines)

    text = str(entry.get("content") or entry.get("output") or "")
    text = _inspect_clean_internal_output_markers(text)
    lines.append(f"{prefix}[{etype}]")
    lines.extend(_inspect_format_block("content", text))
    for ref, full_output in _inspect_entry_full_outputs(entry, output_lookup):
        lines.extend(_inspect_format_block(f"full output {ref}", full_output))
    return "\n".join(lines)


def _inspect_render_timeline(records) -> str:
    """One line per journal record: iter, total_chars, delta, step span, and
    markers for intro/ledger changes (the texts travel only when they changed)."""
    ordered = _inspect_sorted_records(records)
    if not ordered:
        return "no journal records"
    phase = ordered[0].get("phase", "?")
    lines = [f"phase: {phase} — {len(ordered)} journal record(s)"]
    for rec in ordered:
        delta = rec.get("delta") or {}
        markers = []
        if rec.get("intro_text"):
            markers.append("INTRO")
        if rec.get("ledger_text"):
            markers.append("LEDGER")
        marker_str = f"  [{', '.join(markers)}]" if markers else ""
        lines.append(
            f"iter {rec.get('iteration', '?'):>4}  "
            f"chars={rec.get('total_chars', 0):<7} "
            f"added={delta.get('added', 0)} compacted={delta.get('compacted', 0)}  "
            f"span={rec.get('step_span', '?')}{marker_str}"
        )
    return "\n".join(lines)


def _summarize_history_entry(entry, max_chars: int = 200) -> str:
    """One-line thought/action/observation summary for a phase-history entry."""
    return _inspect_format_history_entry(entry)


def _inspect_render_iteration(records, iteration, history_entries, output_lookup=None) -> str:
    """Reconstruct what the model saw at one iteration: the nearest intro text
    at-or-before it, the latest ledger text, the record's manifest, and the
    phase-history entries around it."""
    ordered = _inspect_sorted_records(records)
    target = next((rec for rec in ordered if rec.get("iteration") == iteration), None)
    if target is None:
        known = [rec.get("iteration") for rec in ordered]
        span = f"{known[0]}..{known[-1]}" if known else "none"
        return f"No journal record for iteration {iteration} (recorded iterations: {span})"

    intro_text, intro_iter = None, None
    ledger_text, ledger_iter = None, None
    for rec in ordered:
        it = rec.get("iteration")
        if not isinstance(it, int) or it > iteration:
            continue
        if rec.get("intro_text"):
            intro_text, intro_iter = rec["intro_text"], it
        if rec.get("ledger_text"):
            ledger_text, ledger_iter = rec["ledger_text"], it

    lines = [f"=== iteration {iteration} (phase {target.get('phase', '?')}) ==="]
    manifest = {
        "segments": target.get("segments"),
        "delta": target.get("delta"),
        "total_chars": target.get("total_chars"),
        "step_span": target.get("step_span"),
    }
    lines.append("")
    lines.append("Manifest:")
    lines.append(textwrap.indent(json.dumps(manifest, indent=2, sort_keys=True), "  "))
    if intro_text is not None:
        lines.append("")
        lines.append(f"--- intro (recorded at iter {intro_iter}) ---")
        lines.append(intro_text)
    else:
        lines.append("")
        lines.append("--- intro: none recorded at or before this iteration ---")
    if ledger_text is not None:
        lines.append("")
        lines.append(f"--- ledger (recorded at iter {ledger_iter}) ---")
        lines.append(ledger_text)
    history_entries = [e for e in (history_entries or [])]
    if history_entries:
        lines.append("")
        lines.append(
            f"--- phase history around this iteration ({len(history_entries)} entries) ---"
        )
        for idx, entry in enumerate(history_entries, start=1):
            lines.append(
                _inspect_format_history_entry(entry, index=idx, output_lookup=output_lookup)
            )
    else:
        lines.append("")
        lines.append("--- phase history: no entries available ---")
    return "\n".join(lines)


def _inspect_render_phase_detail(
    records,
    history_entries,
    phase_task: Optional[Dict[str, Any]] = None,
    output_lookup=None,
) -> str:
    """Render one phase with the task summary, journal, and complete branch history."""
    ordered = _inspect_sorted_records(records)
    if not ordered:
        return "no journal records"

    phase = ordered[0].get("phase", "?")
    lines = [f"Phase: {phase}"]

    task = phase_task or {}
    if task:
        status = task.get("status") or "?"
        lines.append(f"Status: {status}")
        notes = " ".join(str(task.get("notes") or "").split())
        key_results = " ".join(str(task.get("key_results") or "").split())
        if key_results:
            lines.append(f"Key results: {key_results}")
        if notes:
            lines.append(f"Notes: {notes}")

    lines.append("")
    lines.append("Journal timeline:")
    lines.append(textwrap.indent(_inspect_render_timeline(ordered), "  "))

    history_entries = [e for e in (history_entries or [])]
    lines.append("")
    lines.append(f"Branch history ({len(history_entries)} entries):")
    if not history_entries:
        lines.append("  no phase history entries available")
    else:
        for idx, entry in enumerate(history_entries, start=1):
            lines.append(
                textwrap.indent(
                    _inspect_format_history_entry(
                        entry,
                        index=idx,
                        output_lookup=output_lookup,
                    ),
                    "  ",
                )
            )
    return "\n".join(lines)


def _inspect_history_window(
    records, iteration, history, before: int = 8, after: int = 2
) -> List[Dict[str, Any]]:
    """Pick the phase-history entries around an iteration. History entries carry
    no iteration marker, so the position is estimated from the cumulative
    journal delta (one `delta.added` ≈ one thought/action entry)."""
    history = [e for e in (history or []) if isinstance(e, dict)]
    if not history:
        return []
    cumulative = 0
    for rec in _inspect_sorted_records(records):
        it = rec.get("iteration")
        if isinstance(it, int) and it <= iteration:
            delta = rec.get("delta") or {}
            added = delta.get("added", 0)
            if isinstance(added, int):
                cumulative += added
    idx = min(cumulative, len(history))
    return history[max(0, idx - before) : min(len(history), idx + after)]


def _parse_journal_records(text: Optional[str]) -> List[Dict[str, Any]]:
    """JSONL → list of dict records; bad lines are skipped, never fatal."""
    records: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _inspect_full_output_records(text: Optional[str]) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict) and record.get("ref_id"):
            records[str(record["ref_id"])] = record
    return records


def _inspect_resolve_phase_for_iteration(
    source, iteration: int
) -> Tuple[str, List[Dict[str, Any]]]:
    phases = source.journal_phases()
    ordered_phases = [p for p in PHASE_NAMES if p in phases]
    ordered_phases += [p for p in phases if p not in ordered_phases]

    for candidate in ordered_phases:
        records = source.journal_records(candidate)
        if any(rec.get("iteration") == iteration for rec in records):
            return candidate, records

    known: List[int] = []
    for candidate in ordered_phases:
        for rec in source.journal_records(candidate):
            value = rec.get("iteration")
            if isinstance(value, int):
                known.append(value)
    if known:
        known = sorted(set(known))
        span = f"{known[0]}..{known[-1]}"
    else:
        span = "none"
    raise _InspectError(f"No journal record for global iteration {iteration} (recorded: {span})")


class _SessionInspectSource:
    """Reads the local `--record` artifact copy under logs/session_*/."""

    def __init__(self, session_dir: str):
        base = Path(session_dir)
        self._full_outputs: Optional[Dict[str, Dict[str, Any]]] = None
        for candidate in (base / ".setup_agent" / "contexts", base / "contexts"):
            if candidate.is_dir():
                self.contexts_dir = candidate
                break
        else:
            raise _InspectError(
                f"No .setup_agent/contexts artifact tree under '{base}' "
                f"(was the run recorded with --record?)"
            )

    def _read(self, relative: str) -> Optional[str]:
        path = self.contexts_dir / relative
        try:
            return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        except OSError:
            return None

    def journal_records(self, phase: str) -> List[Dict[str, Any]]:
        return _parse_journal_records(self._read(f"journal/phase_{phase}.journal.jsonl"))

    def journal_phases(self) -> List[str]:
        journal_dir = self.contexts_dir / "journal"
        if not journal_dir.is_dir():
            return []
        names = sorted(p.name for p in journal_dir.glob("phase_*.journal.jsonl"))
        return [n[len("phase_") : -len(".journal.jsonl")] for n in names]

    def trunk_data(self) -> Optional[Dict[str, Any]]:
        trunks = sorted(self.contexts_dir.glob("trunk_*.json"))
        if not trunks:
            return None
        try:
            return json.loads(trunks[-1].read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def phase_history(self, phase: str) -> List[Any]:
        text = self._read(f"phase_{phase}.json")
        if not text:
            return []
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        return _coerce_entry_list(data.get("history") if isinstance(data, dict) else None)

    def full_output(self, ref: str) -> Optional[str]:
        if self._full_outputs is None:
            self._full_outputs = _inspect_full_output_records(self._read("full_outputs.jsonl"))
        record = self._full_outputs.get(ref) or {}
        output = record.get("output")
        return output if isinstance(output, str) else None


class _ContainerInspectSource:
    """Reads the live in-container context tree via DockerOrchestrator."""

    def __init__(self, docker_name: str):
        label = docker_name[4:] if docker_name.startswith("sag-") else docker_name
        self._full_outputs: Optional[Dict[str, Dict[str, Any]]] = None
        self.orchestrator = DockerOrchestrator(project_name=label)
        if not self.orchestrator.container_exists():
            raise _InspectError(
                f"Docker container '{self.orchestrator.container_name}' not found. "
                f"Use 'sag list' to see available projects, or pass --session logs/session_X."
            )
        if not self.orchestrator.is_container_running():
            raise _InspectError(
                f"Container '{self.orchestrator.container_name}' is not running. "
                f"Start it (e.g. 'sag shell {self.orchestrator.container_name}') "
                f"or inspect a recorded session with --session logs/session_X."
            )

    def _run(self, command: str) -> Optional[str]:
        result = _execute_control(self.orchestrator, command)
        if result.get("exit_code") != 0:
            return None
        return result.get("output", "")

    def journal_records(self, phase: str) -> List[Dict[str, Any]]:
        text = self._run(f"cat {JOURNAL_DIR}/phase_{phase}.journal.jsonl 2>/dev/null")
        return _parse_journal_records(text)

    def journal_phases(self) -> List[str]:
        output = self._run(
            f"find {JOURNAL_DIR} -maxdepth 1 -name 'phase_*.journal.jsonl' -type f 2>/dev/null"
        )
        names = sorted(
            Path(line.strip()).name for line in (output or "").splitlines() if line.strip()
        )
        return [n[len("phase_") : -len(".journal.jsonl")] for n in names]

    def trunk_data(self) -> Optional[Dict[str, Any]]:
        newest = self._run(
            f"find {_CONTEXTS_DIR_IN_CONTAINER} -maxdepth 1 -name 'trunk_*.json' -type f "
            f"2>/dev/null | sort | tail -1"
        )
        newest = (newest or "").strip()
        if not newest:
            return None
        text = self._run(f"cat {newest} 2>/dev/null")
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    def phase_history(self, phase: str) -> List[Any]:
        text = self._run(f"cat {_CONTEXTS_DIR_IN_CONTAINER}/phase_{phase}.json 2>/dev/null")
        if not text:
            return []
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        return _coerce_entry_list(data.get("history") if isinstance(data, dict) else None)

    def full_output(self, ref: str) -> Optional[str]:
        if self._full_outputs is None:
            text = self._run(f"cat {_CONTEXTS_DIR_IN_CONTAINER}/full_outputs.jsonl 2>/dev/null")
            self._full_outputs = _inspect_full_output_records(text)
        record = self._full_outputs.get(ref) or {}
        output = record.get("output")
        return output if isinstance(output, str) else None


def _inspect_render_phase_list(source) -> str:
    """All phases: trunk status (done/blocked) + journal iteration spans."""
    trunk = source.trunk_data()
    phase_tasks: Dict[str, Dict[str, Any]] = {}
    for task in (trunk or {}).get("todo_list", []) or []:
        task_id = task.get("id", "") if isinstance(task, dict) else ""
        if task_id.startswith("phase_"):
            phase_tasks[task_id[len("phase_") :]] = task

    journal_phases = source.journal_phases()
    ordered = [p for p in PHASE_NAMES if p in phase_tasks or p in journal_phases]
    ordered += [p for p in sorted(set(phase_tasks) | set(journal_phases)) if p not in ordered]
    if not ordered:
        raise _InspectError(
            "No phases found: neither phase_* trunk tasks nor journal files "
            "(was this a phase-mode setup run?)"
        )

    status_names = {"completed": "done", "failed": "blocked"}
    lines = ["Phases:"]
    for phase in ordered:
        task = phase_tasks.get(phase, {})
        status = status_names.get(task.get("status"), task.get("status") or "?")
        records = _inspect_sorted_records(source.journal_records(phase))
        iters = [r.get("iteration") for r in records if isinstance(r.get("iteration"), int)]
        span = f"{iters[0]}..{iters[-1]}" if iters else "-"
        lines.append(f"- {phase}: status={status}, iterations={span}")
        key_results = " ".join(str(task.get("key_results") or "").split())
        notes = " ".join(str(task.get("notes") or "").split())
        if key_results:
            lines.append(f"  key results: {key_results}")
        if notes:
            lines.append(f"  notes: {notes}")
    return "\n".join(lines)


@cli.command()
@click.argument("docker_name")
@click.option("--phase", default=None, help=f"Phase to inspect ({'/'.join(PHASE_NAMES)})")
@click.option(
    "--iter",
    "iteration",
    default=None,
    type=int,
    help="Global journal iteration number; phase is optional when journals are available",
)
@click.option(
    "--session",
    "session_dir",
    default=None,
    help="Read from a local --record artifact dir (e.g. logs/session_X) instead of the container",
)
def inspect(docker_name, phase, iteration, session_dir):
    """Inspect recorded context windows: phase timelines and per-iteration views."""
    try:
        if session_dir:
            source = _SessionInspectSource(session_dir)
        else:
            source = _ContainerInspectSource(docker_name)

        if phase is None and iteration is None:
            click.echo(_inspect_render_phase_list(source))
            return

        if phase is None:
            phase, records = _inspect_resolve_phase_for_iteration(source, iteration)
        else:
            phase = _inspect_validate_phase_name(phase)
            records = source.journal_records(phase)

        if not records:
            raise _InspectError(
                f"No journal found for phase '{phase}' "
                f"(expected journal/phase_{phase}.journal.jsonl in the context tree)"
            )
        if iteration is None:
            history = source.phase_history(phase)
            click.echo(
                _inspect_render_phase_detail(
                    records,
                    history,
                    _inspect_phase_task(source, phase),
                    _inspect_output_lookup(source),
                )
            )
            return

        history = source.phase_history(phase)
        entries = _inspect_history_window(records, iteration, history)
        click.echo(
            _inspect_render_iteration(
                records,
                iteration,
                entries,
                _inspect_output_lookup(source),
            )
        )
    except _InspectError as exc:
        console.print(f"[bold red]❌ {exc}[/bold red]")
        sys.exit(1)
    except Exception as exc:  # never a traceback for a debugging command
        logger.debug(f"sag inspect failed: {exc}")
        console.print(f"[bold red]❌ Inspect failed: {exc}[/bold red]")
        sys.exit(1)


def _trajectory_header(document) -> str:
    """One line naming the run, before its turns."""

    session = document.session
    turns = len(document.turns)
    parts = (
        session.project,
        session.run_id,
        f"verdict {session.verdict}" if session.verdict else None,
        f"{turns:,} turn{'' if turns == 1 else 's'}" if turns else None,
    )
    return " · ".join(part for part in parts if part)


def _trajectory_table(session_dir: Path, *, follow: bool) -> None:
    """Replay or tail the session as the turn stream a live run prints.

    One renderer, closed once. A replay feeds the ledger's own lines through
    it; a follow leaves the lines to `follow_trajectory` and renders the deltas
    it yields, including the one that only stopping can produce. The two are
    alternatives, not stages: feeding the whole ledger and then following would
    show every turn twice, the second time after the stream had already said
    what the run finished with.
    """

    document = build_trajectory(session_dir)
    header = _trajectory_header(document)
    if header:
        console.print(header, markup=False, highlight=False)
    stream = TurnStreamRenderer(_turn_stream_sink(), width=console.width, tty=console.is_terminal)
    try:
        if follow:
            tail = follow_trajectory(session_dir)
            try:
                for delta in tail:
                    stream.render_delta(delta)
            finally:
                # Ending the follow is what turns a withheld tail into a torn
                # one, so the last delta is produced by stopping, not by an
                # event. It is rendered on the same terms as every other.
                final = tail.close()
                if final is not None:
                    stream.render_delta(final)
            return
        ledger = control_events_path(session_dir)
        if ledger is not None:
            with ledger.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stream.feed(line)
    finally:
        stream.close()


def _trajectory_json(session_dir: Path, *, follow: bool, detail: str) -> None:
    """Print the document, or one delta per line while the run is still going."""

    if follow:
        stream = follow_trajectory(session_dir, detail=detail)
        try:
            for delta in stream:
                click.echo(delta.model_dump_json())
        finally:
            # Ending the follow is what turns a withheld tail into a torn one,
            # so the last delta is produced by stopping, not by an event. It
            # goes out through the same channel as every other.
            final = stream.close()
            if final is not None:
                click.echo(final.model_dump_json())
        return
    click.echo(build_trajectory(session_dir, detail=detail).model_dump_json())


@cli.command()
@click.argument("session_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("table", "json")),  # a tuple: `list` is a command here
    default="table",
    show_default=True,
    help="table: one line per turn, as the run happened. json: the whole document",
)
@click.option(
    "--follow",
    is_flag=True,
    help="Tail a running session instead of replaying a finished one",
)
@click.option(
    "--detail",
    type=click.Choice(DETAIL_TIERS),  # a tuple: `list` is a command in this module
    default=None,
    help="json only — summary: names, codes, timing, tokens. full: also the bytes each ref names",
)
def trajectory(session_dir, output_format, follow, detail):
    """Print a session's turns: a table to read, or the trajectory document.

    SESSION_DIR is a --record artifact dir (e.g. logs/session_X) or a mirrored
    live session. Nothing in it is written, moved, or locked, and console logs
    are never read: the derivation folds the authoritative ledger only.

    The table is the default because most readers are people. `--format json`
    is what a program reads, and what `| jq` needs.
    """
    if output_format == "table" and detail is not None:
        raise click.UsageError("--detail applies to --format json; the table has one detail tier")
    try:
        if output_format == "json":
            _trajectory_json(session_dir, follow=follow, detail=detail or DETAIL_TIERS[0])
        else:
            _trajectory_table(session_dir, follow=follow)
    except KeyboardInterrupt:
        return  # a follower ends when whoever was watching stops watching
    except (OSError, ValueError) as exc:
        # STDOUT is `--format json`'s contract — `sag trajectory | jq` is the
        # point of it — so a failure puts nothing there and the reason on
        # stderr, beside click's own parser errors. Plain text, not a rich
        # panel: a panel hard-wraps the path it is naming and colours a pipe
        # nobody is reading with a terminal.
        #
        # OSError, not FileNotFoundError: a missing directory is one of many
        # ways the I/O this command does can fail, and `--follow` runs for the
        # length of a run, so the terminal or the mount underneath it can go
        # away mid-stream. Every one of those used to escape as a traceback
        # onto the stream that promised JSON. (Click retires EPIPE on its own.)
        click.echo(f"❌ {exc}", err=True)
        sys.exit(1)


#: A run's own artifacts, by the names it writes them under.
_VERDICT_NAME = "verdict.json"
_RUN_PIN_NAME = "run-pin.json"
_PROJECT_META_NAME = "project_meta.json"
_MODULE_METRICS_NAME = "module_metrics.json"
_REPORT_METRICS_NAME = "report_metrics.json"

#: Where those artifacts sit inside the directory that holds them — at its root
#: or under `.setup_agent/`. The same two roots the trajectory builder reads, so
#: a card and the turns beside it are never read from different copies.
_RUN_ARTIFACT_ROOTS = (".", ".setup_agent")

#: The subdirectory a campaign archive keeps the container's copy in. The
#: archiver names it after the container directory, and `container-evidence` is
#: what it has been called for every archive on disk; the glob beside it covers
#: the rest rather than guessing.
_ARCHIVED_EVIDENCE_DIR = "container-evidence"


def _read_run_document(base: Path, name: str) -> Dict[str, Any] | None:
    """Read one of a run's JSON artifacts, or nothing when it is not readable."""

    for root in _RUN_ARTIFACT_ROOTS:
        path = base / root / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _evidence_bases(directory: Path) -> Tuple[Path, ...]:
    """Where one run's evidence may sit, relative to the directory named.

    A session directory holds it at its own root — that covers both a
    `--record` artifact dir and a `.setup_agent` directory pointed at
    directly. A campaign archive keeps the container's copy one level down,
    beside the rest of the run.
    """

    bases = [directory, directory / _ARCHIVED_EVIDENCE_DIR]
    bases.extend(
        sorted(path.parent.parent for path in directory.glob(f"*/.setup_agent/{_VERDICT_NAME}"))
    )
    ordered: Dict[Path, None] = {}
    for base in bases:
        ordered.setdefault(base, None)
    return tuple(ordered)


def _recorded_run(directory: Path) -> Tuple[Path, Dict[str, Any]] | None:
    """A recorded run's verdict, and the directory the rest of the run sits in.

    The directory comes back with the verdict because everything else the card
    reads — the ledger, the run pin, the metrics — has to come from the same
    copy. Reading the verdict from an archive and the counts from the session
    that no longer holds any would describe two runs as one.
    """

    for base in _evidence_bases(directory):
        if not base.is_dir():
            continue
        payload = _read_run_document(base, _VERDICT_NAME)
        if payload is not None:
            return base, payload
    return None


@cli.command()
@click.argument("target")
@click.option("--json", "as_json", is_flag=True, help="Print the result card as JSON")
def result(target, as_json):
    """Print the result of a finished run, from a container or a session directory.

    TARGET is a container name (sag-<project>) or a recorded session directory:
    a --record artifact dir, or a campaign archive that kept the container's
    copy beside the run. Nothing is written and nothing is re-judged — this
    prints the result the run itself reached.

    It exits 0 whenever a result could be read, whatever that result was.
    Reading a run is not running one, so the run's own verdict is printed
    rather than translated into this command's exit code.
    """

    directory = Path(target)
    found = _recorded_run(directory) if directory.is_dir() else None
    payload: Dict[str, Any] | None = None
    run_pin = module_metrics = report_metrics = None
    project = goal = container = session_dir = None
    counts = _read_run_counts_for_cli(None)

    if found is not None:
        evidence_dir, payload = found
        session_dir = str(directory)
        run_pin = _read_run_document(evidence_dir, _RUN_PIN_NAME)
        module_metrics = _read_run_document(evidence_dir, _MODULE_METRICS_NAME)
        report_metrics = _read_run_document(evidence_dir, _REPORT_METRICS_NAME)
        meta = _read_run_document(evidence_dir, _PROJECT_META_NAME) or {}
        project = meta.get("project_name") or None
        goal = meta.get("goal") or None
        counts = _read_run_counts_for_cli(str(evidence_dir))
    elif not directory.is_dir():
        orchestrator = None
        snapshot = None
        try:
            # By keyword, and without the prefix: the constructor takes its base
            # image first, so a container name handed over positionally opens
            # `sag-default` instead of the container the user asked for.
            orchestrator = DockerOrchestrator(project_name=target.removeprefix("sag-"))
            snapshot = read_live_verdict_snapshot(orchestrator)
        except Exception as exc:  # a reader ends with a sentence, not a traceback
            logger.debug(f"sag result could not open {target}: {exc}")
        if snapshot is not None and snapshot.verdict != "unknown":
            payload = snapshot.model_dump(mode="json")
            container = orchestrator.container_name
            project = orchestrator.project_name
            module_metrics = _read_module_metrics_for_cli(orchestrator)
            report_metrics = _read_metrics_v2_for_cli(orchestrator)

    if payload is None:
        console.print(f"[bold red]❌ no result could be read from {target}[/bold red]")
        sys.exit(1)

    card = build_result_card(
        payload,
        module_metrics=module_metrics,
        report_metrics=report_metrics,
        run_pin=run_pin,
        token_usage=counts["token_usage"],
        trajectory_session=counts["trajectory_session"],
        turn_count=counts["turn_count"],
        tool_calls=counts["tool_calls"],
        tool_failures=counts["tool_failures"],
        project=project,
        goal=goal,
        container=container,
        session_dir=session_dir,
    )
    if as_json:
        click.echo(json.dumps(card.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    # `exit_hint=False`: the block's closing line names the code the run exited
    # with, and this command exits 0 whatever it read. Naming one here would
    # state something the command did not do.
    console.print(render_result_block(card, width=console.width, exit_hint=False))


@cli.command()
@click.argument("docker_name")
@click.option("--force", is_flag=True, help="Force removal without confirmation")
def remove(docker_name, force):
    """Remove a SAG project and its Docker container."""

    try:
        # Extract project name from docker name
        if not docker_name.startswith("sag-"):
            console.print(f"[bold red]❌ Invalid docker name. Must start with 'sag-'[/bold red]")
            return

        project_name = docker_name[4:]  # Remove 'sag-' prefix

        if not force:
            if not click.confirm(
                f"Are you sure you want to remove project '{project_name}' ({docker_name})?"
            ):
                console.print("[yellow]Operation cancelled.[/yellow]")
                return

        console.print(f"[bold red]🗑️ Removing project: {project_name}[/bold red]")

        orchestrator = DockerOrchestrator(project_name=project_name)
        success = orchestrator.remove_project()

        if success:
            console.print(
                f"[bold green]✅ Project '{project_name}' removed successfully![/bold green]"
            )
        else:
            console.print(f"[bold red]❌ Failed to remove project '{project_name}'![/bold red]")

    except Exception as e:
        logger.error(f"Remove project failed: {e}")
        console.print(f"[bold red]❌ Remove failed: {e}[/bold red]")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for the local web UI")
@click.option("--port", default=0, show_default=True, type=int, help="Port for the local web UI")
@click.option(
    "--demo", is_flag=True, help="Use deterministic demo data instead of Docker discovery"
)
def ui(host, port, demo):
    """Start the local SAG Workbench web UI."""
    console.print(f"[bold blue]Starting SAG Workbench[/bold blue] on {host}:{port or 'auto'}")
    run_web_server(host=host, port=port, demo=demo)


@cli.command()
def version():
    """Show SAG version information."""
    console.print(f"[bold blue]SAG[/bold blue] (Setup-Agent) version [green]{__version__}[/green]")
    console.print("[dim]LLM-powered project setup automation[/dim]")


if __name__ == "__main__":
    cli()
