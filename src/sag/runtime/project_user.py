"""The unprivileged account that runs the project's own commands.

Official CI builds and tests a project as an ordinary user, where file
permissions are enforced: a test that makes a file read-only expects the next
write to fail, and as root it does not. When ``project_user`` is configured,
the orchestrator runs the project's own commands (Maven, Gradle, pip, pytest
and the bash tool) as that user and keeps root for its own control work.

Unset, every command runs as root exactly as before, and callers pass no
extra argument at all, so the default path keeps today's call shapes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

SETPRIV = "/usr/bin/setpriv"


def configured_project_user(owner: Any) -> Optional[str]:
    """The project user ``owner`` (an orchestrator) is configured with, or None."""

    user = getattr(getattr(owner, "config", None), "project_user", None)
    return user if isinstance(user, str) and user else None


def project_user_kwargs(orchestrator: Any) -> Dict[str, bool]:
    """Keyword arguments that run one dispatch as the project user.

    Empty when no project user is configured, so the call is exactly today's.
    """

    return {"as_project_user": True} if configured_project_user(orchestrator) else {}


def project_execute(orchestrator: Any, method: Callable[..., Any]) -> Callable[..., Any]:
    """``method`` bound to run as the project user when one is configured."""

    if not configured_project_user(orchestrator):
        return method

    def execute(command: str, *args: Any, **kwargs: Any) -> Any:
        return method(command, *args, as_project_user=True, **kwargs)

    return execute


def drop_privileges_argv(user: str) -> List[str]:
    """The setpriv prefix that turns a root exec into one owned by ``user``.

    The environment is kept (never ``--reset-env``): the Maven witness
    launcher reads ``HOSTNAME`` from it.
    """

    return [SETPRIV, f"--reuid={user}", f"--regid={user}", "--init-groups"]


def project_user_environment(user: str) -> Dict[str, str]:
    """The login identity a process running as ``user`` expects to see."""

    return {"HOME": f"/home/{user}", "USER": user, "LOGNAME": user}


def harness_git_prefix(owner: Any) -> str:
    """Environment prefix for the harness's own ``git status``/``git diff``.

    Git refreshes ``.git/index`` as a side effect of status and diff. Once the
    checkout belongs to the project user, root must not rewrite that file, so
    these read-only calls skip the optional lock. Empty when no project user
    is configured.
    """

    return "GIT_OPTIONAL_LOCKS=0 " if configured_project_user(owner) else ""
