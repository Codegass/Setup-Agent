"""
UI Manager for Setup Agent

Manages the Rich Live display and handles UI events to provide
an interactive, auto-updating CLI interface.
"""

import time
from datetime import datetime
from typing import Optional
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from ui.events import UIEvent, EventType, PhaseType
from ui.components import (
    create_status_panel,
    create_phase_tree,
    create_error_panel,
    create_warning_panel,
    create_success_panel,
    format_duration,
)


class UIManager:
    """
    Manages the UI display and event handling

    Provides a live-updating display using Rich's Live component,
    showing status dashboard, phase tree, and current operations.
    """

    def __init__(self, project_name: str, console: Optional[Console] = None):
        """
        Initialize the UI Manager

        Args:
            project_name: Name of the project being set up
            console: Optional Rich Console instance (creates new one if not provided)
        """
        self.project_name = project_name
        self.console = console or Console()

        # Timing
        self.start_time = time.time()

        # Phase tracking
        self.current_phase: Optional[PhaseType] = None
        self.phases_data = {
            PhaseType.SETUP: {"status": "pending", "steps": []},
            PhaseType.BUILD: {"status": "pending", "steps": []},
            PhaseType.TEST: {"status": "pending", "steps": []},
            PhaseType.VERIFICATION: {"status": "pending", "steps": []},
        }

        # Current status
        self.current_status = "Initializing"
        self.current_step: Optional[str] = None

        # Agent progress tracking
        self.agent_current_step_num: int = 0
        self.agent_current_action: Optional[str] = None  # "thinking", "acting", "observing"
        self.agent_current_tool: Optional[str] = None
        self.agent_tool_params: Optional[dict] = None  # Current tool parameters
        self.agent_detail: Optional[str] = None  # Detailed status like "Using bash tool"

        # Error/warning tracking
        self.errors: list[UIEvent] = []
        self.warnings: list[UIEvent] = []

        # Final result
        self.is_complete = False
        self.final_status: Optional[str] = None

        # Live display
        self.live: Optional[Live] = None

        # Agent step tracking (for collapsible sections)
        self.agent_steps: list[dict] = []
        self.current_agent_step: Optional[dict] = None

    def start(self):
        """Start the live display"""
        self.live = Live(
            self._render_display(),
            console=self.console,
            refresh_per_second=4,
            transient=False
        )
        self.live.start()

    def stop(self):
        """Stop the live display"""
        if self.live:
            self.live.stop()

    def _format_tool_params(self, tool_name: str, params: dict) -> str:
        """
        Format tool parameters for display

        Args:
            tool_name: Name of the tool
            params: Tool parameters dictionary

        Returns:
            Formatted parameter string like "action='analyze'" or "command='mvn clean install'"
        """
        if not params:
            return ""

        # Define which parameters are most important for each tool
        important_params = {
            "bash": ["command"],
            "manage_context": ["action"],
            "file_io": ["action", "path"],
            "maven": ["goal", "action"],
            "gradle": ["task", "action"],
            "project_setup": ["action"],
            "project_analyzer": ["action"],
            "report": ["action"],
        }

        # Get the important params for this tool, or use all params
        params_to_show = important_params.get(tool_name, list(params.keys())[:2])

        formatted_parts = []
        for param in params_to_show:
            if param in params:
                value = params[param]
                # Truncate long values
                value_str = str(value)
                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."
                formatted_parts.append(f"{param}='{value_str}'")

        if formatted_parts:
            return f"({', '.join(formatted_parts)})"
        return ""

    def handle_event(self, event: UIEvent):
        """
        Handle a UI event and update the display

        Args:
            event: The UI event to handle
        """
        # Handle based on event type
        if event.event_type == EventType.PHASE_START:
            self._handle_phase_start(event)
        elif event.event_type == EventType.PHASE_COMPLETE:
            self._handle_phase_complete(event)
        elif event.event_type == EventType.PHASE_ERROR:
            self._handle_phase_error(event)
        elif event.event_type == EventType.STEP_START:
            self._handle_step_start(event)
        elif event.event_type == EventType.STEP_COMPLETE:
            self._handle_step_complete(event)
        elif event.event_type == EventType.STEP_ERROR:
            self._handle_step_error(event)
        elif event.event_type == EventType.STATUS_UPDATE:
            self._handle_status_update(event)
        elif event.event_type == EventType.ERROR:
            self._handle_error(event)
        elif event.event_type == EventType.WARNING:
            self._handle_warning(event)
        elif event.event_type == EventType.SUCCESS:
            self._handle_success(event)
        elif event.event_type == EventType.FAILURE:
            self._handle_failure(event)
        elif event.event_type in [EventType.AGENT_THOUGHT, EventType.AGENT_ACTION, EventType.AGENT_OBSERVATION]:
            self._handle_agent_event(event)

        # Update the display
        self._update_display()

    def _handle_phase_start(self, event: UIEvent):
        """Handle phase start event"""
        self.current_phase = event.phase
        if event.phase:
            self.phases_data[event.phase]["status"] = "running"
        self.current_status = event.message

    def _handle_phase_complete(self, event: UIEvent):
        """Handle phase complete event"""
        if event.phase:
            self.phases_data[event.phase]["status"] = "success"
        self.current_status = event.message

    def _handle_phase_error(self, event: UIEvent):
        """Handle phase error event"""
        if event.phase:
            self.phases_data[event.phase]["status"] = "error"
        self.errors.append(event)

    def _handle_step_start(self, event: UIEvent):
        """Handle step start event"""
        self.current_step = event.message

        if event.phase:
            # Add step to phase
            phase_data = self.phases_data.get(event.phase)
            if phase_data:
                phase_data["steps"].append({
                    "name": event.message,
                    "status": "running",
                    "details": event.details
                })

    def _handle_step_complete(self, event: UIEvent):
        """Handle step complete event"""
        if event.phase:
            # Update step status
            phase_data = self.phases_data.get(event.phase)
            if phase_data and phase_data["steps"]:
                # Find the step by name and update
                for step in phase_data["steps"]:
                    if step["name"] == event.message or step["status"] == "running":
                        step["status"] = "success"
                        if event.details:
                            step["details"] = event.details
                        break

    def _handle_step_error(self, event: UIEvent):
        """Handle step error event"""
        if event.phase:
            # Update step status
            phase_data = self.phases_data.get(event.phase)
            if phase_data and phase_data["steps"]:
                # Find the running step and mark as error
                for step in phase_data["steps"]:
                    if step["status"] == "running":
                        step["status"] = "error"
                        if event.details:
                            step["details"] = event.details
                        break

        self.errors.append(event)

    def _handle_status_update(self, event: UIEvent):
        """Handle status update event"""
        self.current_status = event.message

    def _handle_error(self, event: UIEvent):
        """Handle error event"""
        self.errors.append(event)

    def _handle_warning(self, event: UIEvent):
        """Handle warning event"""
        self.warnings.append(event)

    def _handle_success(self, event: UIEvent):
        """Handle success event"""
        self.is_complete = True
        self.final_status = "success"
        self.current_status = event.message

    def _handle_failure(self, event: UIEvent):
        """Handle failure event"""
        self.is_complete = True
        self.final_status = "failure"
        self.current_status = event.message

    def _handle_agent_event(self, event: UIEvent):
        """Handle agent ReAct events (thought, action, observation)"""
        # Track agent steps for collapsible display
        if event.event_type == EventType.AGENT_THOUGHT:
            # Start a new agent step
            self.agent_current_step_num = event.metadata.get("step_num", self.agent_current_step_num + 1)
            self.agent_current_action = "thinking"
            self.agent_current_tool = None
            self.agent_detail = f"Step {self.agent_current_step_num}: Analyzing situation..."
            self.current_status = "Agent thinking"

            self.current_agent_step = {
                "thought": event.message,
                "action": None,
                "observation": None,
                "status": "running"
            }
            self.agent_steps.append(self.current_agent_step)
        elif event.event_type == EventType.AGENT_ACTION and self.current_agent_step:
            self.agent_current_action = "acting"
            tool_name = event.metadata.get("tool_name", "unknown")
            tool_params = event.metadata.get("tool_params", {})

            self.agent_current_tool = tool_name
            self.agent_tool_params = tool_params

            # Format parameters for display
            params_str = self._format_tool_params(tool_name, tool_params)

            # Build detailed status with parameters
            if params_str:
                self.agent_detail = f"Step {self.agent_current_step_num}: {tool_name} {params_str}"
                self.current_status = f"Using {tool_name} {params_str}"
            else:
                self.agent_detail = f"Step {self.agent_current_step_num}: {tool_name}"
                self.current_status = f"Using {tool_name}"

            self.current_agent_step["action"] = event.message
        elif event.event_type == EventType.AGENT_OBSERVATION and self.current_agent_step:
            self.agent_current_action = "observing"
            self.agent_detail = f"Step {self.agent_current_step_num}: Processing results..."
            self.current_status = "Processing observation"

            self.current_agent_step["observation"] = event.message
            self.current_agent_step["status"] = "complete"
            self.current_agent_step = None

    def _render_display(self):
        """Render the current display"""
        elapsed = time.time() - self.start_time
        elapsed_str = format_duration(elapsed)

        # Build the display elements
        elements = []

        # Determine if we should show spinner
        # Show spinner for: agent working OR setup steps in progress
        show_spinner = False
        if not self.is_complete:
            # Agent is working
            if self.agent_current_action:
                show_spinner = True
            # OR setup phase has running steps
            elif self.current_phase == PhaseType.SETUP:
                setup_phase = self.phases_data.get(PhaseType.SETUP, {})
                if setup_phase.get("status") == "running":
                    show_spinner = True

        # 1. Status panel at the top (clean, single line) with spinner if working
        if show_spinner:
            # Show spinner when actively working
            spinner = Spinner("dots", text=self.current_status, style="cyan")
            status_line = Text()
            status_line.append("SAG", style="bold cyan")
            status_line.append(" │ ", style="dim")
            status_line.append(self.project_name)
            status_line.append(" │ ", style="dim")
            if self.current_phase:
                status_line.append(self.current_phase.value.title())
                status_line.append(" │ ", style="dim")

            # Show agent detail if available, otherwise current status
            if self.agent_detail:
                status_line.append(self.agent_detail, style="yellow")
            else:
                status_line.append(self.current_status, style="yellow")

            status_line.append(" │ ", style="dim")
            status_line.append(elapsed_str, style="blue")

            status_panel = Panel(
                Group(spinner, status_line),
                border_style="cyan",
                padding=(0, 1),
                width=80
            )
        else:
            # Regular status panel without spinner
            status_panel = create_status_panel(
                project_name=self.project_name,
                current_phase=self.current_phase,
                status=self.current_status,
                elapsed_time=elapsed_str
            )
        elements.append(status_panel)
        elements.append("")  # Spacing

        # 2. Phase tree
        phase_tree = create_phase_tree(self.phases_data)
        elements.append(phase_tree)

        # 3. Show errors if any
        if self.errors:
            elements.append("")  # Spacing
            latest_error = self.errors[-1]
            error_panel = create_error_panel(
                latest_error.message,
                details=latest_error.details
            )
            elements.append(error_panel)

        # 4. Show warnings if any
        if self.warnings and not self.errors:  # Only show if no errors
            elements.append("")  # Spacing
            latest_warning = self.warnings[-1]
            warning_panel = create_warning_panel(
                latest_warning.message,
                details=latest_warning.details
            )
            elements.append(warning_panel)

        # 5. Final status if complete
        if self.is_complete and self.final_status == "success":
            elements.append("")  # Spacing
            success_panel = create_success_panel(
                self.current_status,
                summary_items=[
                    ("Total time", elapsed_str),
                    ("Phases completed", f"{sum(1 for p in self.phases_data.values() if p['status'] == 'success')}/4")
                ]
            )
            elements.append(success_panel)

        return Group(*elements)

    def _update_display(self):
        """Update the live display"""
        if self.live:
            self.live.update(self._render_display())

    def display_final_summary(self):
        """Display final summary with expandable sections"""
        self.stop()

        # Print final status
        self.console.print()
        self.console.print("=" * 60)
        self.console.print()

        elapsed = time.time() - self.start_time
        elapsed_str = format_duration(elapsed)

        if self.final_status == "success":
            success_panel = create_success_panel(
                self.current_status,
                summary_items=[
                    ("Total time", elapsed_str),
                    ("Phases completed", f"{sum(1 for p in self.phases_data.values() if p['status'] == 'success')}/4")
                ]
            )
            self.console.print(success_panel)
        elif self.final_status == "failure":
            if self.errors:
                latest_error = self.errors[-1]
                error_panel = create_error_panel(
                    latest_error.message,
                    details=latest_error.details
                )
                self.console.print(error_panel)

        # Print detailed phase tree with all steps expanded
        self.console.print()
        self.console.print(Panel("📋 Detailed Execution Log", border_style="cyan"))
        self.console.print()
        phase_tree = create_phase_tree(self.phases_data)
        self.console.print(phase_tree)

        # Print agent steps if any (collapsible summary)
        if self.agent_steps:
            self.console.print()
            self.console.print(Panel(
                f"🤖 Agent executed {len(self.agent_steps)} ReAct steps",
                border_style="blue"
            ))
            self.console.print("[dim]Run with --verbose to see detailed agent reasoning[/dim]")

        self.console.print()
        self.console.print("=" * 60)
        self.console.print()
