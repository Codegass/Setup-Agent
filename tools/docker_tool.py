import docker
from docker.errors import APIError, ContainerError, ImageNotFound
import logging

class DockerManager:
    def __init__(self, default_image="alpine:latest", default_command="sleep 3600"):
        self.client = docker.from_env()
        self.default_image = default_image  # 默认映像设置
        self.default_command = default_command  # 默认命令设置
        logging.basicConfig(level=logging.INFO)  # 设置日志级别 # 储存logger的输出

    def pull_image(self, image_name):
        """拉取指定的 Docker 映像"""
        try:
            logging.info(f"Pulling image {image_name}...")
            self.client.images.pull(image_name)
            logging.info("Image pulled successfully.")
        except ImageNotFound:
            logging.error("Image not found.")
        except APIError as e:
            logging.error(f"Server error occurred: {e}")

    def create_and_run_container(self, image_name=None, command=None):
        """创建并运行一个 Docker 容器"""
        image_name = image_name or self.default_image
        command = command or self.default_command
        try:
            logging.info(f"Creating and starting container from image {image_name}...")
            container = self.client.containers.run(image_name, command, detach=True)
            logging.info(f"Container {container.id} created and started.")
            return container
        except ContainerError as e:
            logging.error(f"Container error: {e}")
        except ImageNotFound:
            logging.error("Image not found, please pull the image first.")
        except APIError as e:
            logging.error(f"Server error occurred: {e}")

    def execute_command_in_container(self, container, command):
        """在指定的 Docker 容器中执行命令"""
        try:
            logging.info(f"Executing command in container {container.id}...")
            exec_id = self.client.api.exec_create(container.id, cmd=command)
            output = self.client.api.exec_start(exec_id)
            logging.info("Command executed. Output:")
            logging.info(output.decode('utf-8'))
        except APIError as e:
            logging.error(f"Error executing command: {e}")

    def remove_container(self, container, force=False):
        """移除指定的 Docker 容器"""
        try:
            logging.info(f"Removing container {container.id}...")
            container.remove(force=force)
            logging.info("Container removed successfully.")
        except APIError as e:
            logging.error(f"Error removing container: {e}")

# Example usage
if __name__ == "__main__":
    manager = DockerManager()
    # Creating a container with the default image
    container = manager.create_and_run_container()
    manager.execute_command_in_container(container, "echo 'Hello from default container!'")
    manager.remove_container(container, force=True)
    # Creating a container with a specified image
    manager.pull_image("python:3.8-slim")
    container = manager.create_and_run_container("python:3.8-slim", "sleep 3600")
    manager.execute_command_in_container(container, "echo Hello, world!")
    manager.remove_container(container, force=True)  # Force removal if necessary
