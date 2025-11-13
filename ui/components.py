"""
Rich component builders for UI elements

Provides reusable functions to create Rich components like panels, trees,
progress indicators, and status displays.
"""

from typing import Optional
from rich.panel import Panel
from rich.tree import Tree
from rich.table import Table
from rich.text import Text
from rich.console import Group
from rich.padding import Padding

from ui.events import PhaseType


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
    extra_info: Optional[str] = None
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
    content = f"[bold cyan]SAG[/bold cyan] │ {project_name} │ {phase_text} │ {status} │ {elapsed_time}"

    return Panel(
        content,
        border_style="cyan",
        padding=(0, 1),
        width=80  # Fixed width for consistent display
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

    return Panel(
        content,
        title="Error",
        border_style="red",
        padding=(1, 2)
    )


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

    return Panel(
        content,
        title="Warning",
        border_style="yellow",
        padding=(1, 2)
    )


def create_success_panel(
    message: str,
    summary_items: Optional[list[tuple[str, str]]] = None
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

    return Panel(
        content,
        title="Success",
        border_style="green",
        padding=(1, 2)
    )


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

    return Panel(
        content,
        title="Information",
        border_style="blue",
        padding=(1, 2)
    )


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
