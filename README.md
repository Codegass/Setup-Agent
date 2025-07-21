# SAG (Setup-Agent)
🤖 **An LLM-Powered Engine for Automated Project Setup & Configuration** 🤖

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**SAG (Setup-Agent)** is an advanced AI agent designed to fully automate the initial setup, configuration, and ongoing tasks for any software project. It operates within an isolated Docker environment, intelligently interacting with project files, shell commands, and web resources to transform hours—or even days—of manual setup into a process that takes just a few minutes.

---

## 📖 Philosophy: Solving the "Getting Started" Problem

In software development, configuring a new project—especially a large open-source one—is often a tedious, time-consuming, and error-prone task. Developers must read extensive documentation, resolve dependency conflicts, and understand the project's structure before writing a single line of effective code.

**SAG's core mission is to solve this problem.** It aims to be an intelligent "Project Initialization Specialist" by adhering to these core principles:

- **Complete Isolation**: All operations occur within Docker containers, ensuring the host machine is never polluted. This guarantees a clean, reproducible setup every time.
- **Intelligent Planning & Execution**: SAG doesn't just execute commands; it analyzes, plans (by creating a TODO list), and systematically solves problems like a human expert.
- **Hierarchical Context Management**: With an innovative "Trunk/Branch" context system, SAG can handle complex task chains, from high-level goals to low-level operations, without getting lost in long-running processes.
- **Dual-Model Collaboration**: It leverages the strengths of different LLMs—one for deep thinking and planning (e.g., `o1-preview`) and another for fast action and tool use (e.g., `gpt-4o`)—to achieve a balance of efficiency and effectiveness.

## ✨ Core Concepts

### 1. Dual-Model ReAct Engine

The "brain" of SAG is an enhanced ReAct (Reasoning-Acting) engine. By separating the "thinking" and "acting" phases and using different models for each, it achieves more effective decision-making:

- **Thinking Model**: Responsible for analyzing complex problems, creating high-level plans, and learning from errors. It thinks deeper and sees further. Supports advanced reasoning features like OpenAI's `o1` models and Anthropic's Claude thinking capabilities.
- **Action Model**: Responsible for precisely executing the plan laid out by the thinking model, whether that's calling a tool, generating code, or running a command. It's focused on "doing."

This architecture allows SAG to be both thoughtful and agile when tackling unfamiliar projects.

### 2. Hierarchical Context Management (Trunk & Branch)

To solve the problem of context loss in complex tasks, SAG implements a hierarchical context management system:

- **Trunk Context**:
  - Stores the project's overall goal, the complete TODO list, and high-level progress.
  - Acts as the "command center" for the entire setup task.

- **Branch Context**:
  - Created for each specific task on the TODO list.
  - Contains all the details, logs, and the current focus for that sub-task.
  - The agent works within a branch context to solve one problem at a time before returning to the trunk.

This system enables SAG to switch between high-level planning and low-level execution seamlessly, ensuring stability and coherence throughout long-running tasks.

### 3. Hierarchical Tool-belt Design

At first glance, a single `bash` tool could handle all system interactions. However, relying solely on a low-level tool would force the AI agent to manage immense complexity, from remembering command syntax to parsing raw text output—a process that is both inefficient and error-prone.

SAG adopts a hierarchical tool-belt design to address this, creating layers of abstraction that empower the agent to work more intelligently:

-   **Low-Level Foundational Tools**: At the base is the `BashTool`. It provides unrestricted, granular control, much like an assembly language for system operations. It is the ultimate fallback for tasks that have no specialized tool.

-   **Mid-Level Specialized Tools**: These tools encapsulate domain-specific knowledge. For example:
    -   `SystemTool` understands system package management (`apt-get`), abstracting away the need to manually form `install` or `update` commands.
    -   `MavenTool` is an expert in Java's Maven build system. It knows about goals, profiles, and properties, and can intelligently parse Maven's verbose output to determine if a build succeeded, failed, or had test errors.

-   **High-Level Workflow Tools**: At the top layer, tools like `ProjectSetupTool` orchestrate complex, multi-step workflows. Its `clone` action doesn't just run `git clone`; it also automatically detects the project type (Maven, Node.js, Python), suggests the next appropriate actions, and can even trigger dependency installation, compressing a long chain of human-like reasoning into a single, intent-driven command.

This layered approach allows the agent to delegate complexity. Instead of figuring out *how* to do something with basic commands, it can focus on *what* it needs to achieve, leading to faster, more reliable, and more sophisticated automation.

## 🏗️ System Architecture

SAG is composed of several core components:

1.  **CLI (`main.py`)**: The user's entry point for interacting with SAG, providing commands like `project`, `run`, and `list`.
2.  **Config System (`config/`)**: Manages all configurations via a `.env` file, including API keys and model parameters for multiple LLM providers.
3.  **Setup Agent (`agent/agent.py`)**: The main orchestrator that directs the entire project setup workflow.
4.  **ReAct Engine (`agent/react_engine.py`)**: Implements the think-act loop, calls LLMs, and parses their responses.
5.  **Context Manager (`agent/context_manager.py`)**: Maintains and persists the Trunk/Branch contexts.
6.  **Tool-belt (`tools/`)**: Provides fundamental capabilities like Bash execution, file I/O, and web search.
7.  **Docker Orchestrator (`docker_orch/orch.py`)**: Manages the lifecycle of containers and volumes, ensuring environmental isolation.
8.  **LiteLLM**: Acts as a unified API gateway to communicate with all supported LLM services (OpenAI, Anthropic, Ollama, etc.).

## 🚀 Quick Start

### 1. Prerequisites
- [Docker](https://www.docker.com/)
- [Python 3.10+](https://www.python.org/)
- [uv](https://github.com/astral-sh/uv) (The recommended Python package manager)

### 2. Installation & Configuration

```bash
# 1. Clone the repository
git clone https://github.com/your-org/Setup-Agent.git
cd Setup-Agent

# 2. Install dependencies with uv (this will also create a virtual environment)
uv sync

# 3. Create and edit your configuration file
cp .env.example .env
nano .env  # Fill in your API keys and other settings
```

### 3. Basic Usage

```bash
# Start setting up a new project
sag project https://github.com/fastapi/fastapi.git

# List all managed projects and their status
sag list

# Run a new task on an existing project
sag run sag-fastapi --task "add a new endpoint to handle /healthz"

# Access the project container's shell
sag shell sag-fastapi

# Remove a project (including its container and volume)
sag remove sag-fastapi
```

## 🛠️ CLI Command Reference

SAG provides a clean and powerful set of CLI commands.

| Command | Description | Example |
|---|---|---|
| `sag project <url>` | Initializes the setup for a new project from a Git repository URL. | `sag project https://github.com/pallets/flask.git` |
| `sag list` | Lists all projects managed by SAG, showing their container name, status, and last comment. | `sag list` |
| `sag run <name>` | Runs a specified task on an existing project. | `sag run sag-flask --task "add unit tests for the application factory"` |
| `sag shell <name>` | Connects to an interactive shell inside the specified project's container. | `sag shell sag-flask` |
| `sag remove <name>` | Permanently deletes a project, including its container and data volume. | `sag remove sag-flask --force` |
| `sag version` | Displays SAG's version information. | `sag version` |
| `sag --help` | Shows the help message. | `sag --help` |

**Global Options:**
- `--log-level [DEBUG|INFO|...]`: Overrides the log level set in the `.env` file.
- `--log-file <path>`: Specifies a custom path for the log file.

## ⚙️ Configuration Explained

All configuration is managed through the `.env` file in the project's root directory.

**Key Configuration Options:**
- `SAG_THINKING_MODEL`: The "thinking model" for planning and analysis. A powerful model is recommended (e.g., `o1-preview`, `claude-sonnet-4-20250514`).
- `SAG_ACTION_MODEL`: The "action model" for task execution. A fast and cost-effective model is recommended (e.g., `gpt-4o`, `claude-3-5-sonnet-20240620`).
- `SAG_THINKING_PROVIDER`: The provider for the thinking model (`openai`, `anthropic`, etc.).
- `SAG_REASONING_EFFORT`: For thinking models, controls reasoning depth (`low`, `medium`, `high`).
- `SAG_THINKING_BUDGET_TOKENS`: For Claude models, controls thinking budget (1024, 2048, 4096).
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.: API keys for the respective LLM providers.
- `SAG_LOG_LEVEL`: Sets the logging verbosity. `DEBUG` mode is highly detailed and includes LiteLLM's internal logs.
- `SAG_MAX_ITERATIONS`: The maximum number of iterations for a single `run` or `project` command to prevent infinite loops.

## 🔍 How It Works: A Look Under the Hood

When you run `sag project <url>`, a sophisticated sequence of events is triggered:

1.  **Environment Initialization**: SAG's Docker Orchestrator spins up an isolated Docker container and a persistent data volume.
2.  **Project Cloning**: The agent uses its `bash` tool inside the container to clone the specified Git repository.
3.  **Context Establishment**: A **Trunk Context** is created with the high-level goal (e.g., "Set up this project to be runnable").
4.  **Intelligent Analysis & Planning**: The **Thinking Model** is engaged to analyze the project structure (e.g., `README.md`, `package.json`, `pyproject.toml`) and generate a comprehensive TODO list, which is stored in the Trunk Context.
5.  **Task Loop Initiation**:
    a. The agent picks the first task from the Trunk Context's TODO list.
    b. A **Branch Context** is created to focus exclusively on this task (e.g., "Install project dependencies").
    c. The **Action Model** executes the necessary steps within the Branch Context (e.g., runs `npm install`).
    d. The result is observed. If an error occurs, the **Thinking Model** is re-engaged to analyze the cause and find a solution (e.g., using the `web_search` tool to look up the error message).
    e. Once the task is complete, the agent records a summary in the Trunk Context, marks the task as "completed," and destroys the Branch Context.
6.  **Rinse and Repeat**: Step 5 is repeated until all tasks in the TODO list are completed.
7.  **Completion**: The agent exits, leaving behind a fully configured and runnable project environment in the Docker container.

## 🎯 Use Cases

- **Rapid Prototyping**: Set up and run any open-source project in minutes to evaluate its suitability.
- **Standardized Dev Environments**: Create consistent, one-click development environments for team members.
- **CI/CD Automation**: Automate complex project setups and testing environments in your CI pipelines.
- **Learning New Technologies**: Quickly get hands-on with an unfamiliar framework or stack by letting SAG handle the setup.
- **Secure Experimentation**: Safely test unfamiliar or untrusted code in an isolated sandbox.

## 🤝 Contributing

We warmly welcome contributions of all kinds! Whether it's a bug report, a feature suggestion, or a pull request, your help is invaluable to the project.

## 📝 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.