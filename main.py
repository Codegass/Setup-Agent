"""Main CLI interface for SAG (Setup-Agent)."""

import sys
from pathlib import Path
from typing import Optional

import click
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import get_config, set_config, Config, LogLevel
from docker_orch.orch import DockerOrchestrator
from agent.agent import SetupAgent


console = Console()


@click.group()
@click.option(
    "--log-level",
    type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR']),
    help="Set the logging level"
)
@click.option(
    "--log-file", 
    type=click.Path(),
    help="Path to log file"
)
@click.pass_context
def cli(ctx, log_level, log_file):
    """SAG: Setup-Agent - LLM Powered project setup automation."""
    
    # Create configuration
    config = Config.from_env()
    
    # Override with CLI options if provided
    if log_level:
        config.log_level = LogLevel(log_level)
    if log_file:
        config.log_file = log_file
    
    # Set global config
    set_config(config)
    
    # Ensure context object exists
    ctx.ensure_object(dict)
    ctx.obj['config'] = config
    
    # Display welcome message for main commands
    if ctx.invoked_subcommand not in ['list']:
        console.print(Panel.fit(
            "[bold blue]SAG[/bold blue] - [dim]Setup Agent[/dim]\n"
            "[dim]Automated project setup with AI[/dim]",
            border_style="blue"
        ))


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
            status = project['status']
            if status == 'running':
                status_text = Text("🟢 running", style="green")
            elif status == 'exited':
                status_text = Text("🔴 stopped", style="red")
            else:
                status_text = Text(f"🟡 {status}", style="yellow")
            
            # Get last comment from agent
            last_comment = project.get('last_comment', 'No comment available')
            if len(last_comment) > 50:
                last_comment = last_comment[:47] + "..."
            
            table.add_row(
                project['project_name'],
                project['docker_name'],
                status_text,
                last_comment,
                project['created']
            )
        
        console.print(table)
        console.print(f"\n[dim]Use 'sag run <docker_name> --task \"description\"' to continue working on a project.[/dim]")
        
    except Exception as e:
        logger.error(f"List projects failed: {e}")
        console.print(f"[bold red]❌ Failed to list projects: {e}[/bold red]")


@cli.command()
@click.argument('repo_url')
@click.option(
    '--name',
    help="Override project name (default: extracted from URL)"
)
@click.option(
    '--goal',
    help="Custom setup goal (default: auto-generated)"
)
@click.pass_context
def project(ctx, repo_url, name, goal):
    """Initial setup for a new project from repository URL."""
    
    config = ctx.obj['config']
    
    try:
        # Extract project name from URL if not provided
        if not name:
            name = repo_url.split('/')[-1].replace('.git', '')
        
        # Generate default goal if not provided
        if not goal:
            goal = f"Setup and configure the {name} project to be runnable"
        
        docker_name = f"sag-{name}"
        
        console.print(f"[bold green]🚀 Setting up new project[/bold green]")
        console.print(f"[dim]Repository:[/dim] {repo_url}")
        console.print(f"[dim]Project Name:[/dim] {name}")
        console.print(f"[dim]Docker Name:[/dim] {docker_name}")
        console.print(f"[dim]Goal:[/dim] {goal}")
        
        # Check if project already exists
        orchestrator = DockerOrchestrator(project_name=name)
        if orchestrator.container_exists():
            console.print(f"[bold yellow]⚠️ Project '{name}' already exists![/bold yellow]")
            console.print(f"[dim]Use 'sag run {docker_name} --task \"description\"' to continue working on it.[/dim]")
            return
        
        # Initialize agent
        agent = SetupAgent(
            config=config,
            orchestrator=orchestrator,
            max_iterations=config.max_iterations
        )
        
        # Run the setup
        success = agent.setup_project(
            project_url=repo_url,
            project_name=name,
            goal=goal
        )
        
        if success:
            console.print(f"[bold green]✅ Project '{name}' setup completed![/bold green]")
            console.print(f"\n[dim]Next steps:[/dim]")
            console.print(f"  sag run {docker_name} --task \"run the application\"")
            console.print(f"  sag run {docker_name} --task \"add tests\"")
            console.print(f"  docker exec -it {docker_name} /bin/bash")
        else:
            console.print(f"[bold red]❌ Project setup failed![/bold red]")
            console.print(f"[dim]Check logs for details. You can retry with:[/dim]")
            console.print(f"  sag run {docker_name} --task \"continue setup\"")
            
    except Exception as e:
        logger.error(f"Project setup failed: {e}")
        console.print(f"[bold red]❌ Setup failed: {e}[/bold red]")
        sys.exit(1)


@cli.command()
@click.argument('docker_name')
@click.option(
    '--task',
    required=True,
    help="Specific task or requirement for the agent"
)
@click.option(
    '--max-iterations',
    default=30,
    type=int,
    help="Maximum number of agent iterations"
)
@click.pass_context
def run(ctx, docker_name, task, max_iterations):
    """Run a specific task on an existing SAG project."""
    
    config = ctx.obj['config']
    
    try:
        # Extract project name from docker name
        if not docker_name.startswith('sag-'):
            console.print(f"[bold red]❌ Invalid docker name. Must start with 'sag-'[/bold red]")
            console.print(f"[dim]Use 'sag list' to see available projects.[/dim]")
            return
        
        project_name = docker_name[4:]  # Remove 'sag-' prefix
        
        console.print(f"[bold green]🔧 Running task on project: {project_name}[/bold green]")
        console.print(f"[dim]Docker:[/dim] {docker_name}")
        console.print(f"[dim]Task:[/dim] {task}")
        
        # Initialize orchestrator
        orchestrator = DockerOrchestrator(project_name=project_name)
        
        # Check if container exists
        if not orchestrator.container_exists():
            console.print(f"[bold red]❌ Docker container '{docker_name}' not found![/bold red]")
            console.print(f"[dim]Use 'sag list' to see available projects.[/dim]")
            return
        
        # Initialize agent
        agent = SetupAgent(
            config=config,
            orchestrator=orchestrator,
            max_iterations=max_iterations
        )
        
        # Run the task
        success = agent.run_task(
            project_name=project_name,
            task_description=task
        )
        
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
@click.argument('docker_name')
@click.option(
    '--shell',
    default="/bin/bash",
    help="Shell to use in the container"
)
def shell(docker_name, shell):
    """Connect to a project's Docker container shell."""
    
    try:
        # Extract project name from docker name
        if not docker_name.startswith('sag-'):
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


@cli.command()
@click.argument('docker_name')
@click.option(
    '--force',
    is_flag=True,
    help="Force removal without confirmation"
)
def remove(docker_name, force):
    """Remove a SAG project and its Docker container."""
    
    try:
        # Extract project name from docker name
        if not docker_name.startswith('sag-'):
            console.print(f"[bold red]❌ Invalid docker name. Must start with 'sag-'[/bold red]")
            return
        
        project_name = docker_name[4:]  # Remove 'sag-' prefix
        
        if not force:
            if not click.confirm(f"Are you sure you want to remove project '{project_name}' ({docker_name})?"):
                console.print("[yellow]Operation cancelled.[/yellow]")
                return
        
        console.print(f"[bold red]🗑️ Removing project: {project_name}[/bold red]")
        
        orchestrator = DockerOrchestrator(project_name=project_name)
        success = orchestrator.remove_project()
        
        if success:
            console.print(f"[bold green]✅ Project '{project_name}' removed successfully![/bold green]")
        else:
            console.print(f"[bold red]❌ Failed to remove project '{project_name}'![/bold red]")
            
    except Exception as e:
        logger.error(f"Remove project failed: {e}")
        console.print(f"[bold red]❌ Remove failed: {e}[/bold red]")


@cli.command()
def version():
    """Show SAG version information."""
    console.print("[bold blue]SAG[/bold blue] (Setup-Agent) version [green]0.2.0[/green]")
    console.print("[dim]LLM-powered project setup automation[/dim]")


if __name__ == "__main__":
    cli() 