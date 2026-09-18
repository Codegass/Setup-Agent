"""A probe's answer is not a failure.

`_execute_command_with_logging` runs `test -d X`, `cat X 2>/dev/null`,
`grep -q …` for callers that branch on the result. The first real container run
printed `❌ checking Maven launcher directory failed (exit_code=1)` six times,
and `❌ reading settings.gradle failed` twice, for files the project does not
have — fourteen warnings, every one of them the probe's normal negative answer.
The helper cannot know what a non-zero exit means; only its caller can, and
every caller in the file handles the negative. So the helper states the exit
and leaves the meaning to the caller.
"""

from loguru import logger

from sag.agent.physical_validator import PhysicalValidator


class _ContainerThatSaysNo:
    def execute_command(self, command, **_kwargs):
        return {"exit_code": 1, "output": ""}


def _capture():
    seen: list[tuple[str, str]] = []
    handle = logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    return seen, handle


def test_a_probe_answering_no_is_logged_as_an_answer_and_not_as_a_failure():
    seen, handle = _capture()
    try:
        validator = PhysicalValidator(
            docker_orchestrator=_ContainerThatSaysNo(), project_path="/workspace"
        )
        result = validator._execute_command_with_logging(
            "test -d /workspace/.mvn", "checking Maven launcher directory"
        )
    finally:
        logger.remove(handle)

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert [level for level, _ in seen if level in {"WARNING", "ERROR"}] == [], seen
    stated = [
        message
        for level, message in seen
        if level == "DEBUG" and "checking Maven launcher directory" in message
    ]
    assert stated, seen
    assert "exit 1" in stated[0]
    assert "failed" not in stated[0]
    assert "❌" not in stated[0]
