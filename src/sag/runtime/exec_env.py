"""Default runtime process environment for SAG-managed containers."""

from __future__ import annotations

from typing import Dict, Optional


DEFAULT_UTF8_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


def default_utf8_environment(environment: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return SAG's UTF-8 defaults merged with caller-provided environment."""
    exec_env = dict(DEFAULT_UTF8_ENVIRONMENT)
    if environment:
        exec_env.update(environment)
    return exec_env
