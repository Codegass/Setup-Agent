"""Internal tool implementations — NOT model-facing.

These modules back the registered facades (build, project, search) and shared
infrastructure; they are never registered with the agent directly. The
model-facing surface lives one level up: bash, file_io, context_tool,
build/, project_tool, search_tool, report_tool.
"""
