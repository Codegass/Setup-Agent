"""Tools package for Setup-Agent.

Model-facing tools live at this level (bash, file_io, context, build,
project, search, report); implementation delegates and shared infrastructure
live in `internal/` and are never registered with the agent directly.

The package initializer intentionally does not import every tool. Low-level
modules import ``sag.tools.base`` while evidence/output modules are still being
initialized; eager convenience imports make that harmless submodule import
cycle back through Maven/Gradle and the half-built caller.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import BaseTool, ToolError, ToolResult
    from .bash import BashTool
    from .build import BuildTool
    from .context_tool import ContextTool
    from .file_io import FileIOTool
    from .internal.env_tool import EnvTool
    from .internal.gradle_tool import GradleTool
    from .internal.maven_tool import MavenTool
    from .internal.output_search_tool import OutputSearchTool
    from .internal.project_setup_tool import ProjectSetupTool
    from .internal.system_tool import SystemTool
    from .internal.web_search import WebSearchTool
    from .project_tool import ProjectTool
    from .report_tool import ReportTool
    from .search_tool import SearchTool

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolError",
    "BashTool",
    "FileIOTool",
    "BuildTool",
    "ProjectTool",
    "SearchTool",
    "WebSearchTool",
    "ContextTool",
    "EnvTool",
    "MavenTool",
    "GradleTool",
    "ProjectSetupTool",
    "SystemTool",
    "ReportTool",
    "OutputSearchTool",
]

_EXPORT_MODULES = {
    "BaseTool": ".base",
    "ToolResult": ".base",
    "ToolError": ".base",
    "BashTool": ".bash",
    "FileIOTool": ".file_io",
    "BuildTool": ".build",
    "ProjectTool": ".project_tool",
    "SearchTool": ".search_tool",
    "WebSearchTool": ".internal.web_search",
    "ContextTool": ".context_tool",
    "EnvTool": ".internal.env_tool",
    "MavenTool": ".internal.maven_tool",
    "GradleTool": ".internal.gradle_tool",
    "ProjectSetupTool": ".internal.project_setup_tool",
    "SystemTool": ".internal.system_tool",
    "ReportTool": ".report_tool",
    "OutputSearchTool": ".internal.output_search_tool",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
