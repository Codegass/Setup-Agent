"""Main CLI interface for SAG (Setup-Agent)."""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
from sag.config import (
    Config,
    LogLevel,
    ensure_session_logging,
    get_config,
    get_session_logger,
    set_config,
    suppress_console_logging,
)
from sag.coverage.runner import apply_coverage
from sag.docker_orch.orch import DockerOrchestrator
from sag.utils.git_utils import extract_project_name_from_url
from sag.web.server import run_web_server

console = Console()

# Note: You may see "Exception ignored while finalizing... ValueError: I/O operation on closed file"
# at the end of execution. This is a harmless cleanup issue from urllib3/docker-py during
# garbage collection and does not affect functionality. Python already handles it gracefully.


def _start_agent_session_logging(config: Config) -> None:
    """Initialize session logging at the point a command starts agent work."""
    session_logger = ensure_session_logging(config)
    if config.verbose and session_logger:
        logger.info(f"Session ID: {session_logger.session_id}")
        logger.info(f"Logs directory: {session_logger.session_log_dir}")


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
        result = orchestrator.execute_command("ls -d /workspace/*/ 2>/dev/null | head -10")

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
            check_result = orchestrator.execute_command(
                f"test -f {dir_path}/pom.xml || test -f {dir_path}/build.gradle || "
                f"test -f {dir_path}/package.json || test -f {dir_path}/requirements.txt || "
                f"test -f {dir_path}/pyproject.toml && echo FOUND || echo NOTFOUND"
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
    """
    Read project metadata from /workspace/.setup_agent/project_meta.json.

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
        result = orchestrator.execute_command(
            "cat /workspace/.setup_agent/project_meta.json 2>/dev/null"
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


def _save_setup_artifacts(orchestrator: DockerOrchestrator, project_name: str) -> None:
    """Copy setup artifacts from Docker container to local session logs.

    Args:
        orchestrator: Docker orchestrator for the project
        project_name: Name of the project
    """
    try:
        session_logger = get_session_logger()
        if not session_logger:
            logger.warning("No session logger available, skipping artifact save")
            return

        # Get the session log directory
        session_dir = session_logger.session_log_dir
        if not session_dir.exists():
            session_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving artifacts to {session_dir}")

        # Check if .setup_agent folder exists in container
        check_result = orchestrator.execute_command(
            "test -d /workspace/.setup_agent && echo 'EXISTS' || echo 'NOT_FOUND'"
        )

        if check_result.get("output", "").strip() == "EXISTS":
            # Copy .setup_agent folder
            copy_cmd = (
                f"docker cp {orchestrator.container_name}:/workspace/.setup_agent {session_dir}/"
            )
            import subprocess

            result = subprocess.run(copy_cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("✅ Copied .setup_agent folder from container")
            else:
                logger.warning(f"Failed to copy .setup_agent folder: {result.stderr}")
        else:
            logger.info(".setup_agent folder not found in container, skipping")

        # Find and copy setup-report-*.md files
        find_result = orchestrator.execute_command(
            "find /workspace -maxdepth 1 -name 'setup-report-*.md' -type f 2>/dev/null | head -10"
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
                    else:
                        logger.warning(f"Failed to copy {filename}: {result.stderr}")
        else:
            logger.info("No setup report files found in container")

        console.print(f"[dim]Artifacts saved to: {session_dir}[/dim]")

    except Exception as e:
        logger.error(f"Failed to save artifacts: {e}")
        # Don't fail the main operation if artifact saving fails
        console.print(f"[yellow]⚠️ Could not save artifacts: {e}[/yellow]")


def _detect_coverage_build_system(orchestrator, project_dir: str):
    """Detect maven/gradle physically for the coverage pass (or None)."""
    try:
        from sag.agent.physical_validator import PhysicalValidator

        bs = PhysicalValidator(docker_orchestrator=orchestrator)._detect_build_system(project_dir)
        return bs if bs in ("maven", "gradle") else None
    except Exception as exc:
        logger.debug(f"coverage build-system detect failed: {exc}")
        return None


def _run_coverage_pass(orchestrator, project_name: str) -> bool:
    """Isolated, best-effort coverage pass AFTER the setup verdict is locked.

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
        wrote = apply_coverage(orchestrator, project_dir, build_system)
        # Pollution guard (warn-only): tracked source files must be unchanged.
        dirty = orchestrator.execute_command(
            f"cd {project_dir} && git status --porcelain 2>/dev/null "
            f"| grep -vE 'target/|build/|\\.setup_agent' | head -5"
        )
        if (dirty.get("output") or "").strip():
            logger.warning(f"Coverage pass left source-tree changes:\n{dirty['output']}")
        return wrote
    except Exception as exc:  # never propagate into the command's success/exit path
        logger.warning(f"Coverage pass failed (best-effort, ignored): {exc}")
        return False


@click.group()
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    help="Set the logging level",
)
@click.option("--log-file", type=click.Path(), help="Path to log file")
@click.option("--verbose", is_flag=True, help="Enable verbose debugging output with detailed logs")
@click.option("--ui", is_flag=True, help="Enable enhanced UI mode with live progress display")
@click.pass_context
def cli(ctx, log_level, log_file, verbose, ui):
    """SAG: Setup-Agent - LLM Powered project setup automation."""

    # Check for mutually exclusive flags
    if verbose and ui:
        console.print(
            "[bold red]❌ Error: --verbose and --ui flags cannot be used together[/bold red]"
        )
        console.print("[dim]Please choose one:[/dim]")
        console.print("  --verbose : Detailed console logs for debugging")
        console.print("  --ui      : Clean interactive UI with live updates")
        sys.exit(1)

    # Create configuration
    config = Config.from_env()

    # Override with CLI options if provided
    if log_level:
        config.log_level = LogLevel(log_level)
    if log_file:
        config.log_file = log_file
    if verbose:
        config.verbose = verbose
    if ui:
        config.ui_mode = ui

    # Set global config without opening a session log. Session logs are for
    # agent executions only; read-only CLI commands should not create
    # logs/session_* directories.
    set_config(config, initialize_logging=False, quiet_console=log_level is None)

    # Ensure context object exists
    ctx.ensure_object(dict)
    ctx.obj["config"] = config

    # Display welcome message for main commands (skip in UI mode, will be shown by UIManager)
    if ctx.invoked_subcommand not in ["list"] and not config.verbose and not config.ui_mode:
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
    "--coverage", is_flag=True, help="Run an isolated JaCoCo coverage pass after setup (best-effort)"
)
@click.option("--ui", is_flag=True, help="Enable enhanced UI mode with live progress display")
@click.option(
    "--ref",
    "project_ref",
    help="Git ref to set up, such as a branch, tag, release tag, short commit, or full commit.",
)
@click.pass_context
def project(ctx, repo_url, name, goal, record, coverage, ui, project_ref):
    """Initial setup for a new project from repository URL."""

    config = ctx.obj["config"]

    # Override ui_mode from command-line flag if provided
    if ui:
        # Check for mutual exclusion with verbose
        if config.verbose:
            console.print(
                "[bold red]❌ Error: --verbose and --ui flags cannot be used together[/bold red]"
            )
            console.print("[dim]Please choose one:[/dim]")
            console.print("  --verbose : Detailed console logs for debugging")
            console.print("  --ui      : Clean interactive UI with live updates")
            sys.exit(1)
        config.ui_mode = ui
        # Suppress console logging for UI mode
        suppress_console_logging()

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

        # Only show project setup details in non-UI mode
        if not config.ui_mode:
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
            # Always show critical errors/warnings, even in UI mode
            console.print(
                f"[bold yellow]⚠️ Container '{docker_name}' already exists![/bold yellow]"
            )
            console.print(
                f"[dim]Use 'sag run {docker_name} --task \"description\"' to continue working on it.[/dim]"
            )
            return

        _start_agent_session_logging(config)

        # Initialize agent
        agent = SetupAgent(config=config, orchestrator=orchestrator)

        # Run the setup - pass project_name (from URL) and docker_label for metadata
        success = agent.setup_project(
            project_url=repo_url,
            project_name=project_name,
            goal=goal,
            docker_label=docker_label,
            project_ref=project_ref,
        )

        # Save artifacts if recording is enabled
        if record:
            _save_setup_artifacts(orchestrator, project_name)

        if coverage:
            _run_coverage_pass(orchestrator, project_name)

        # Only show completion messages in non-UI mode (UI manager handles this)
        if not config.ui_mode:
            if success:
                console.print(
                    f"[bold green]✅ Project '{project_name}' setup completed![/bold green]"
                )
                console.print(f"\n[dim]Next steps:[/dim]")
                console.print(f'  uv run sag run {docker_name} --task "run the application"')
                console.print(f'  uv run sag run {docker_name} --task "add tests"')
                console.print(f"  uv run sag shell {docker_name}")
            else:
                console.print(f"[bold red]❌ Project setup failed![/bold red]")
                console.print(f"[dim]Check logs for details. You can retry with:[/dim]")
                console.print(f'  sag run {docker_name} --task "continue setup"')

        if not success:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Project setup failed: {e}")
        # Always show critical errors, even in UI mode
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
    "--coverage", is_flag=True, help="Run an isolated JaCoCo coverage pass after setup (best-effort)"
)
@click.option("--ui", is_flag=True, help="Enable enhanced UI mode with live progress display")
@click.pass_context
def run(ctx, docker_name, task, max_iterations, record, coverage, ui):
    """Run a specific task on an existing SAG project."""

    config = ctx.obj["config"]

    # Override ui_mode from command-line flag if provided
    if ui:
        # Check for mutual exclusion with verbose
        if config.verbose:
            console.print(
                "[bold red]❌ Error: --verbose and --ui flags cannot be used together[/bold red]"
            )
            console.print("[dim]Please choose one:[/dim]")
            console.print("  --verbose : Detailed console logs for debugging")
            console.print("  --ui      : Clean interactive UI with live updates")
            sys.exit(1)
        config.ui_mode = ui
        # Suppress console logging for UI mode
        suppress_console_logging()

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

        # Try to read project metadata first (preferred method)
        # This was saved during 'sag project' setup
        metadata = read_project_metadata(orchestrator)

        if metadata:
            # Use project_name from metadata - this is the actual directory name
            actual_project_name = metadata.get("project_name", docker_label)
            if actual_project_name != docker_label:
                console.print(
                    f"[dim]Note:[/dim] Container '{docker_name}' contains project '{actual_project_name}'"
                )
        else:
            # Fallback: probe the container to find the project directory
            logger.info("No project metadata found, falling back to directory detection")
            detected = detect_project_directory_in_container(orchestrator)
            if detected and detected != docker_label:
                actual_project_name = detected
                console.print(
                    f"[dim]Note:[/dim] Detected project directory: /workspace/{actual_project_name}"
                )
            else:
                # Last resort: use docker_label as project_name
                actual_project_name = docker_label

        # Only show task info in non-UI mode (UI manager handles this)
        if not config.ui_mode:
            console.print(
                f"[bold green]🔧 Running task on project: {actual_project_name}[/bold green]"
            )
            console.print(f"[dim]Docker:[/dim] {docker_name}")
            console.print(f"[dim]Task:[/dim] {task}")
            if record:
                console.print(f"[dim]Recording:[/dim] Enabled (artifacts will be saved locally)")

        _start_agent_session_logging(config)

        # Initialize agent
        final_max_iterations = (
            max_iterations if max_iterations is not None else config.max_iterations
        )
        agent = SetupAgent(
            config=config, orchestrator=orchestrator, max_iterations=final_max_iterations
        )

        # Run the task with the actual project name
        success = agent.run_task(project_name=actual_project_name, task_description=task)

        # Save artifacts if recording is enabled
        if record:
            _save_setup_artifacts(orchestrator, actual_project_name)

        if coverage:
            _run_coverage_pass(orchestrator, actual_project_name)

        # Only show completion messages in non-UI mode (UI manager handles this)
        if not config.ui_mode:
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
        result = self.orchestrator.execute_command(command)
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
