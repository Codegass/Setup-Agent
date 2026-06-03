"""
Rich component builders for UI elements

Provides reusable functions to create Rich components like panels, trees,
progress indicators, and status displays.
"""

from typing import Optional

from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from sag.ui.diagnosis import FinalDiagnosis
from sag.ui.events import PhaseType
from sag.ui.state import UIRunState, UITimelineEntry

# Status icons
ICONS = {
    "pending": "⏳",
    "running": "⏳",
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
}

# Phase icons
PHASE_ICONS = {
    PhaseType.SETUP: "📦",
    PhaseType.BUILD: "🔨",
    PhaseType.TEST: "🧪",
    PhaseType.VERIFICATION: "✓",
}


def create_status_panel(
    project_name: str,
    current_phase: Optional[PhaseType],
    status: str,
    elapsed_time: str,
    extra_info: Optional[str] = None,
) -> Panel:
    """
    Create the top status dashboard panel

    Args:
        project_name: Name of the project being set up
        current_phase: Current phase of execution
        status: Current status text
        elapsed_time: Elapsed time string
        extra_info: Optional additional information (not used for cleaner display)

    Returns:
        Rich Panel with status information
    """
    phase_text = current_phase.value.title() if current_phase else "Initializing"

    # Build clean single-line status (fixed width, no extra_info)
    content = (
        f"[bold cyan]SAG[/bold cyan] │ {project_name} │ {phase_text} │ {status} │ {elapsed_time}"
    )

    return Panel(
        content, border_style="cyan", padding=(0, 1), width=80  # Fixed width for consistent display
    )


def create_phase_tree(phases_data: dict) -> Tree:
    """
    Create a tree view of phases and their steps

    Args:
        phases_data: Dictionary mapping PhaseType to phase info
            {
                PhaseType.SETUP: {
                    "status": "success",  # pending, running, success, error
                    "steps": [
                        {"name": "Docker Environment", "status": "success", "details": "..."},
                        {"name": "Project Analysis", "status": "running", "details": "..."},
                    ]
                },
                ...
            }

    Returns:
        Rich Tree with phase hierarchy
    """
    tree = Tree("", hide_root=True)

    for phase_type in [PhaseType.SETUP, PhaseType.BUILD, PhaseType.TEST, PhaseType.VERIFICATION]:
        phase_info = phases_data.get(phase_type, {"status": "pending", "steps": []})
        phase_status = phase_info.get("status", "pending")
        phase_steps = phase_info.get("steps", [])

        # Phase icon and status
        icon = PHASE_ICONS.get(phase_type, "•")
        status_icon = ICONS.get(phase_status, "")

        # Phase name with status
        if phase_status == "success":
            phase_style = "green"
        elif phase_status == "running":
            phase_style = "yellow"
        elif phase_status == "error":
            phase_style = "red"
        else:
            phase_style = "dim"

        phase_name = f"{icon} {phase_type.value.title()}"
        if phase_status != "pending":
            phase_name = f"{phase_name} {status_icon}"

        phase_node = tree.add(Text(phase_name, style=phase_style))

        # Add steps
        for step in phase_steps:
            step_name = step.get("name", "Unknown")
            step_status = step.get("status", "pending")
            step_details = step.get("details")

            step_icon = ICONS.get(step_status, "•")

            if step_status == "success":
                step_style = "green"
            elif step_status == "running":
                step_style = "yellow"
            elif step_status == "error":
                step_style = "red"
            else:
                step_style = "dim"

            step_text = f"{step_icon} {step_name}"
            step_node = phase_node.add(Text(step_text, style=step_style))

            # Add details as sub-node if available
            if step_details and step_status in ["success", "error"]:
                step_node.add(Text(step_details, style="dim"))

    return tree


def create_status_header(state: UIRunState, elapsed_time: str) -> Panel:
    """Create the snapshot-based status header."""
    phase_text = state.current_phase.value.title() if state.current_phase else "Initializing"
    content = (
        f"[bold cyan]SAG[/bold cyan] │ {state.project_name} │ "
        f"{phase_text} │ {state.current_status} │ {elapsed_time}"
    )
    return Panel(content, border_style="cyan", padding=(0, 1), width=80)


def create_phase_timeline(state: UIRunState) -> Tree:
    """Create a snapshot-based phase timeline tree."""
    tree = Tree("[bold]Phase Timeline[/bold]")

    for phase in state.phases:
        icon = PHASE_ICONS.get(phase.phase, "•")
        status_icon = ICONS.get(phase.status, "")
        label = f"{icon} {phase.phase.value.title()}"
        if phase.status != "pending" and status_icon:
            label = f"{label} {status_icon}"
        node = tree.add(Text(label, style=_status_style(phase.status)))

        for step in phase.steps:
            step_status = str(step.get("status", "pending"))
            step_icon = ICONS.get(step_status, "•")
            step_name = str(step.get("name", "Unknown"))
            step_node = node.add(Text(f"{step_icon} {step_name}", style=_status_style(step_status)))
            details = step.get("details")
            if details and step_status in {"success", "error"}:
                step_node.add(Text(str(details), style="dim"))

    return tree


def create_active_operation_panel(state: UIRunState) -> Panel | None:
    """Create a concise active-operation panel when a tool is in flight."""
    operation = state.active_operation
    if not operation.tool_name:
        return None

    lines = [f"[cyan]Tool:[/cyan] {operation.tool_name}"]
    if operation.visible_params:
        lines.append(f"[cyan]Params:[/cyan] {operation.visible_params}")
    elif operation.action:
        lines.append(f"[cyan]Action:[/cyan] {operation.action}")
    if operation.workdir:
        lines.append(f"[cyan]Workdir:[/cyan] {operation.workdir}")
    if operation.detail:
        lines.append(f"[cyan]Detail:[/cyan] {operation.detail}")

    return Panel("\n".join(lines), title="Active Operation", border_style="blue", padding=(1, 2))


def create_recent_timeline_panel(state: UIRunState, limit: int = 6) -> Panel | None:
    """Create a compact recent timeline panel."""
    entries = state.timeline[-limit:]
    if not entries:
        return None

    content = "\n".join(_format_timeline_line(entry) for entry in entries)
    return Panel(content, title="Timeline", border_style="cyan", padding=(1, 2))


def create_recovery_panel(state: UIRunState) -> Panel | None:
    """Create a recovery panel when recovery state is present."""
    recovery = state.recovery
    if not recovery.active:
        return None

    lines = []
    if recovery.message:
        lines.append(recovery.message)
    if recovery.strategy:
        lines.append(f"[cyan]Strategy:[/cyan] {recovery.strategy}")
    if recovery.retry_count:
        lines.append(f"[cyan]Retries:[/cyan] {recovery.retry_count}")
    if recovery.unresolved_risk:
        lines.append(f"[cyan]Risk:[/cyan] {recovery.unresolved_risk}")

    return Panel("\n".join(lines), title="Recovery", border_style="yellow", padding=(1, 2))


def create_evidence_panel(state: UIRunState, limit: int = 5) -> Panel | None:
    """Create a compact evidence panel."""
    records = state.evidence[-limit:]
    if not records:
        return None

    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column()
    table.add_column(style="dim")

    for record in records:
        path = record.path or ""
        table.add_row(record.kind, record.summary, path)

    return Panel(table, title="Evidence", border_style="green", padding=(1, 2))


def create_final_diagnosis_panel(diagnosis: FinalDiagnosis) -> Panel:
    """Create a final diagnosis panel from the typed diagnosis result."""
    border_style = "green" if diagnosis.status == "success" else "red"
    title = "Final Diagnosis" if diagnosis.status != "success" else "Success"
    lines = [diagnosis.outcome]

    if diagnosis.failures:
        lines.append(f"[red]Latest failure:[/red] {diagnosis.failures[-1]}")
    if diagnosis.warnings:
        lines.append(f"[yellow]Latest warning:[/yellow] {diagnosis.warnings[-1]}")
    if diagnosis.recovery:
        lines.append(f"[cyan]Recovery:[/cyan] {diagnosis.recovery[-1]}")
    if diagnosis.evidence:
        lines.append(f"[cyan]Evidence:[/cyan] {diagnosis.evidence[-1]}")
    if diagnosis.next_actions:
        lines.append(f"[cyan]Next action:[/cyan] {diagnosis.next_actions[0]}")

    return Panel("\n".join(lines), title=title, border_style=border_style, padding=(1, 2))


def create_error_panel(error_message: str, details: Optional[str] = None) -> Panel:
    """
    Create an error panel

    Args:
        error_message: Main error message
        details: Optional detailed error information

    Returns:
        Rich Panel with error information
    """
    content = f"❌ [bold red]{error_message}[/bold red]"

    if details:
        content = f"{content}\n\n[dim]{details}[/dim]"

    return Panel(content, title="Error", border_style="red", padding=(1, 2))


def create_warning_panel(warning_message: str, details: Optional[str] = None) -> Panel:
    """
    Create a warning panel

    Args:
        warning_message: Main warning message
        details: Optional detailed warning information

    Returns:
        Rich Panel with warning information
    """
    content = f"⚠️  [bold yellow]{warning_message}[/bold yellow]"

    if details:
        content = f"{content}\n\n[dim]{details}[/dim]"

    return Panel(content, title="Warning", border_style="yellow", padding=(1, 2))


def create_success_panel(
    message: str, summary_items: Optional[list[tuple[str, str]]] = None
) -> Panel:
    """
    Create a success panel

    Args:
        message: Main success message
        summary_items: Optional list of (label, value) tuples for summary

    Returns:
        Rich Panel with success information
    """
    content = f"✅ [bold green]{message}[/bold green]"

    if summary_items:
        content += "\n\n"
        for label, value in summary_items:
            content += f"  [cyan]{label}:[/cyan] {value}\n"

    return Panel(content, title="Success", border_style="green", padding=(1, 2))


def create_info_panel(message: str, items: Optional[list[str]] = None) -> Panel:
    """
    Create an info panel

    Args:
        message: Main info message
        items: Optional list of info items

    Returns:
        Rich Panel with info
    """
    content = f"ℹ️  {message}"

    if items:
        content += "\n\n"
        for item in items:
            content += f"  • {item}\n"

    return Panel(content, title="Information", border_style="blue", padding=(1, 2))


def format_duration(seconds: float) -> str:
    """
    Format duration in seconds to human-readable string

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string like "2m 34s" or "45s"
    """
    if seconds < 60:
        return f"{int(seconds)}s"

    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)

    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"

    hours = int(minutes // 60)
    remaining_minutes = int(minutes % 60)

    return f"{hours}h {remaining_minutes}m {remaining_seconds}s"


def _status_style(status: str) -> str:
    if status == "success":
        return "green"
    if status == "running":
        return "yellow"
    if status == "error":
        return "red"
    return "dim"


def _format_timeline_line(entry: UITimelineEntry) -> str:
    timestamp = entry.timestamp.strftime("%H:%M:%S")
    kind = str(entry.kind).replace("_", " ").title()
    line = f"[dim]{timestamp}[/dim] [cyan]{kind}[/cyan] {entry.message}"
    if entry.details:
        line = f"{line} [dim]{entry.details}[/dim]"
    return line
