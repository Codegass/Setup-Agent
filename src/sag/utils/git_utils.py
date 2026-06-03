"""Git URL utilities — shared between CLI and tools."""

import re
from urllib.parse import urlparse


def extract_project_name_from_url(repo_url: str) -> str:
    """Extract the project name from a Git repository URL.

    Supports HTTPS/HTTP, file://, SSH (``git@host:user/repo.git``), Azure
    DevOps (``.../_git/repo``), and local paths. The ``.git`` suffix is
    stripped from the returned name.

    Raises ``ValueError`` when the URL is empty or no name can be derived.
    """
    if not repo_url:
        raise ValueError("Repository URL cannot be empty")

    url = repo_url.strip()

    if "\\" in url:
        parts = [p for p in url.replace("\\", "/").split("/") if p]
        if parts:
            return parts[-1].removesuffix(".git")

    # SSH URLs: git@host:user/repo.git
    ssh_match = re.match(r"^git@[^:]+:(.+)$", url)
    if ssh_match:
        repo_name = ssh_match.group(1).split("/")[-1]
        return repo_name.removesuffix(".git")

    # Azure DevOps: https://dev.azure.com/org/project/_git/repo
    azure_match = re.match(r".*/_git/([^/]+)/?$", url)
    if azure_match:
        return azure_match.group(1).removesuffix(".git")

    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if path:
            return path.split("/")[-1].removesuffix(".git")
    except Exception:
        pass

    # Fallback for malformed URLs / Windows-style local paths.
    parts = [p for p in url.replace("\\", "/").split("/") if p]
    if parts:
        return parts[-1].removesuffix(".git")

    raise ValueError(f"Could not extract project name from URL: {repo_url}")
