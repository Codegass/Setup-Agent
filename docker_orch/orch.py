"""Docker Orchestrator for managing containers and volumes."""

import os
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound
from loguru import logger

from config import get_config


class DockerOrchestrator:
    """Orchestrates Docker containers for project setup."""

    def __init__(self, base_image: str = None, project_name: str = None):
        self.config = get_config()
        self.base_image = base_image or self.config.docker_base_image
        self.project_name = project_name

        # Docker client
        try:
            self.client = docker.from_env()
            self.client.ping()  # Test connection
            logger.info("Docker client initialized successfully")
        except DockerException as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            raise

        # Container names (SAG naming convention)
        if self.project_name:
            self.container_name = f"sag-{self.project_name}"
            self.volume_name = f"sag-{self.project_name}-vol"
        else:
            self.container_name = "sag-default"
            self.volume_name = "sag-default-vol"

        logger.info(f"Docker Orchestrator initialized for project: {project_name}")

    def create_and_start_container(self) -> bool:
        """Create and start a new container for the project."""

        if not self.project_name:
            raise ValueError("Project name is required to create container")

        try:
            # Check if container already exists
            if self.container_exists():
                logger.info(f"Container {self.container_name} already exists")
                if not self.is_container_running():
                    logger.info("Starting existing container")
                    return self.start_container()
                return True

            # Create volume if it doesn't exist
            # Skip volume creation - we're not using volumes anymore
            # if not self._volume_exists():
            #     self._create_volume()

            # Prepare container configuration
            container_config = self._get_container_config()

            logger.info(f"Creating container {self.container_name} with image {self.base_image}")

            # Create container
            container = self.client.containers.create(
                image=self.base_image, name=self.container_name, **container_config
            )

            # Start container
            container.start()

            # Wait for container to be ready
            if self._wait_for_container_ready():
                logger.info(f"Container {self.container_name} created and started successfully")

                # Setup the container environment
                self._setup_container_environment()

                return True
            else:
                logger.error("Container failed to become ready")
                return False

        except Exception as e:
            logger.error(f"Failed to create and start container: {e}")
            return False

    def start_container(self) -> bool:
        """Start an existing container."""

        try:
            container = self.client.containers.get(self.container_name)

            if container.status == "running":
                logger.info(f"Container {self.container_name} is already running")
                return True

            logger.info(f"Starting container {self.container_name}")
            container.start()

            if self._wait_for_container_ready():
                logger.info(f"Container {self.container_name} started successfully")
                return True
            else:
                logger.error("Container failed to become ready after start")
                return False

        except NotFound:
            logger.error(f"Container {self.container_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to start container: {e}")
            return False

    def stop_container(self) -> bool:
        """Stop the container."""

        try:
            container = self.client.containers.get(self.container_name)

            if container.status != "running":
                logger.info(f"Container {self.container_name} is not running")
                return True

            logger.info(f"Stopping container {self.container_name}")
            container.stop(timeout=30)

            logger.info(f"Container {self.container_name} stopped successfully")
            return True

        except NotFound:
            logger.error(f"Container {self.container_name} not found")
            return False
        except Exception as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    def remove_project(self) -> bool:
        """Remove the project container and volume."""

        try:
            success = True

            # Stop and remove container
            if self.container_exists():
                container = self.client.containers.get(self.container_name)

                if container.status == "running":
                    logger.info(f"Stopping container {self.container_name}")
                    container.stop(timeout=30)

                logger.info(f"Removing container {self.container_name}")
                container.remove()

            # Remove volume
            # Skip volume removal - we're not using volumes anymore
            # if self._volume_exists():
            #     logger.info(f"Removing volume {self.volume_name}")
            #     volume = self.client.volumes.get(self.volume_name)
            #     volume.remove()

            logger.info(f"Project {self.project_name} removed successfully")
            return success

        except Exception as e:
            logger.error(f"Failed to remove project: {e}")
            return False

    def container_exists(self) -> bool:
        """Check if the container exists."""

        try:
            self.client.containers.get(self.container_name)
            return True
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking container existence: {e}")
            return False

    def is_container_running(self) -> bool:
        """Check if the container is running."""

        try:
            container = self.client.containers.get(self.container_name)
            return container.status == "running"
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking container status: {e}")
            return False

    def connect_to_container(self, shell: str = "/bin/bash") -> None:
        """Connect to the container interactively."""

        if not self.is_container_running():
            raise RuntimeError(f"Container {self.container_name} is not running")

        # Use docker exec to connect interactively
        cmd = ["docker", "exec", "-it", self.container_name, shell]

        logger.info(f"Connecting to container with command: {' '.join(cmd)}")

        try:
            # Replace current process with docker exec
            os.execvp("docker", cmd)
        except Exception as e:
            logger.error(f"Failed to connect to container: {e}")
            raise

    def execute_command(self, command: str, workdir: str = None) -> Dict[str, Any]:
        """Execute a command in the container."""

        try:
            container = self.client.containers.get(self.container_name)

            if container.status != "running":
                raise RuntimeError(f"Container {self.container_name} is not running")

            # Execute command
            result = container.exec_run(
                command, workdir=workdir or self.config.workspace_path, stdout=True, stderr=True
            )

            return {
                "exit_code": result.exit_code,
                "output": result.output.decode("utf-8") if result.output else "",
                "success": result.exit_code == 0,
            }

        except Exception as e:
            logger.error(f"Failed to execute command in container: {e}")
            return {"exit_code": -1, "output": "", "error": str(e), "success": False}

    def get_container_info(self) -> Optional[Dict[str, Any]]:
        """Get container information."""

        try:
            if not self.container_exists():
                return None

            container = self.client.containers.get(self.container_name)

            return {
                "name": container.name,
                "status": container.status,
                "image": container.image.tags[0] if container.image.tags else "unknown",
                "created": container.attrs["Created"],
                "ports": container.ports,
                "mounts": [
                    mount["Source"] + ":" + mount["Destination"]
                    for mount in container.attrs.get("Mounts", [])
                ],
                "workspace_path": self.config.workspace_path,
            }

        except Exception as e:
            logger.error(f"Failed to get container info: {e}")
            return None

    def get_detailed_status(self) -> Dict[str, Any]:
        """Get detailed status information."""

        status = {
            "project_name": self.project_name,
            "container_name": self.container_name,
            "volume_name": self.volume_name,
            "container_exists": self.container_exists(),
            "container_running": self.is_container_running(),
            "volume_exists": self._volume_exists(),
        }

        if status["container_exists"]:
            container_info = self.get_container_info()
            if container_info:
                status.update(container_info)

        return status

    def list_sag_projects(self) -> List[Dict[str, Any]]:
        """List all SAG projects with their status and last comment."""

        projects = []

        try:
            # Get all containers with sag- prefix
            containers = self.client.containers.list(all=True)

            for container in containers:
                if container.name.startswith("sag-"):
                    project_name = container.name.replace("sag-", "")

                    # Get container info
                    created = container.attrs.get("Created", "")
                    if created:
                        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        created_str = created_dt.strftime("%Y-%m-%d %H:%M")
                    else:
                        created_str = "Unknown"

                    # Get last comment from volume file
                    temp_orch = DockerOrchestrator(project_name=project_name)
                    last_comment = temp_orch.get_last_comment_from_volume()

                    projects.append(
                        {
                            "project_name": project_name,
                            "docker_name": container.name,
                            "status": container.status,
                            "created": created_str,
                            "image": container.image.tags[0] if container.image.tags else "Unknown",
                            "last_comment": last_comment,
                        }
                    )

            # Sort by creation time (newest first)
            projects.sort(key=lambda x: x["created"], reverse=True)

            return projects

        except Exception as e:
            logger.error(f"Failed to list SAG projects: {e}")
            return []

    def update_last_comment(self, comment: str) -> bool:
        """Update the last comment for the project container."""

        try:
            if not self.container_exists():
                logger.warning(f"Container {self.container_name} does not exist")
                return False

            container = self.client.containers.get(self.container_name)

            # Get current labels
            current_labels = container.attrs.get("Config", {}).get("Labels", {}) or {}

            # Update the last comment label
            current_labels["sag.last_comment"] = comment
            current_labels["sag.last_update"] = datetime.now().isoformat()

            # Commit the container with updated labels
            # Note: This creates a new image, but we'll use a simple approach
            # by stopping and recreating the container with new labels

            logger.info(f"Updated last comment for {self.container_name}: {comment}")

            # For now, we'll store the comment in a volume file
            # This is simpler than recreating the container
            self._store_comment_in_volume(comment)

            return True

        except Exception as e:
            logger.error(f"Failed to update last comment: {e}")
            return False

    def _store_comment_in_volume(self, comment: str) -> bool:
        """Store the comment in a file within the container volume."""

        try:
            if not self.is_container_running():
                self.start_container()

            # Create a comment file in the workspace
            comment_data = {
                "comment": comment,
                "timestamp": datetime.now().isoformat(),
                "project": self.project_name,
            }

            import json

            comment_json = json.dumps(comment_data, indent=2)

            # Write to container
            result = self.execute_command(
                f"echo '{comment_json}' > {self.config.workspace_path}/.sag_last_comment.json"
            )

            if result.get("success", False):
                logger.debug(f"Comment stored successfully for {self.project_name}")
                return True
            else:
                logger.warning(f"Failed to store comment: {result.get('error', 'Unknown error')}")
                return False

        except Exception as e:
            logger.error(f"Failed to store comment in volume: {e}")
            return False

    def get_last_comment_from_volume(self) -> str:
        """Get the last comment from the volume file."""

        try:
            if not self.is_container_running():
                return "Container not running"

            # Read comment file from container
            result = self.execute_command(
                f"cat {self.config.workspace_path}/.sag_last_comment.json 2>/dev/null || echo '{{}}'"
            )

            if result.get("success", False):
                import json

                try:
                    comment_data = json.loads(result.get("output", "{}"))
                    return comment_data.get("comment", "No comment available")
                except json.JSONDecodeError:
                    return "No comment available"
            else:
                return "No comment available"

        except Exception as e:
            logger.error(f"Failed to get comment from volume: {e}")
            return "Error reading comment"


    ## TODO：Need to add default java and python related config 
    def _get_container_config(self) -> Dict[str, Any]:
        """Get container configuration."""

        config = {
            "detach": True,
            "stdin_open": True,
            "tty": True,
            "working_dir": self.config.workspace_path,
            # Remove volume mount - files will be stored inside container
            # "volumes": {self.volume_name: {"bind": self.config.workspace_path, "mode": "rw"}},
            "environment": {"DEBIAN_FRONTEND": "noninteractive", "TERM": "xterm-256color"},
            "labels": {
                "setup-agent.project": self.project_name,
                "setup-agent.created": datetime.now().isoformat(),
            },
            # Add a command to keep container running
            "command": ["/bin/bash", "-c", f"mkdir -p {self.config.workspace_path} && while true; do sleep 30; done"],
        }

        return config

    def _create_volume(self) -> bool:
        """Create a Docker volume for the project."""

        try:
            logger.info(f"Creating volume {self.volume_name}")

            self.client.volumes.create(
                name=self.volume_name,
                labels={
                    "setup-agent.project": self.project_name,
                    "setup-agent.created": datetime.now().isoformat(),
                },
            )

            logger.info(f"Volume {self.volume_name} created successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to create volume: {e}")
            return False

    def _volume_exists(self) -> bool:
        """Check if the volume exists."""

        try:
            self.client.volumes.get(self.volume_name)
            return True
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error checking volume existence: {e}")
            return False

    def _wait_for_container_ready(self, timeout: int = 30) -> bool:
        """Wait for container to be ready."""

        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                container = self.client.containers.get(self.container_name)

                if container.status == "running":
                    # Test if we can execute a simple command
                    result = container.exec_run("echo 'ready'", stdout=True, stderr=True)
                    if result.exit_code == 0:
                        return True

                time.sleep(1)

            except Exception as e:
                logger.debug(f"Container not ready yet: {e}")
                time.sleep(1)

        logger.error(
            f"Container {self.container_name} did not become ready within {timeout} seconds"
        )
        return False

    def _setup_container_environment(self) -> bool:
        """Setup the basic environment in the container."""

        try:
            logger.info("Setting up container environment")

            # Update package lists and install basic tools
            setup_commands = [
                "apt-get update",
                "apt-get install -y curl wget git nano vim python3 python3-pip nodejs npm build-essential",
                f"mkdir -p {self.config.workspace_path}",
                f"chown -R root:root {self.config.workspace_path}",
            ]

            for command in setup_commands:
                result = self.execute_command(command)
                if not result["success"]:
                    logger.warning(f"Setup command failed: {command}")
                    logger.warning(f"Output: {result.get('output', '')}")
                    # Continue with other commands even if one fails

            logger.info("Container environment setup completed")
            return True

        except Exception as e:
            logger.error(f"Failed to setup container environment: {e}")
            return False
