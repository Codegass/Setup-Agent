# tests/test_wire_schema_openai_constraints.py
"""Model-facing tool schemas obey the OpenAI function-calling top-level rule.

Live lp-commons-dbcp/lp-cayenne (2026-08-09, second attempt): the first model
request of the run failed with `Invalid schema for function 'project': schema
must have type 'object' and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'const'/
'not' at the top level.` The facade's env-branch conditional default rode in
an `allOf` — wire sugar for a default the execute layer already enforces
(activate is forced True, explicit False is refused). No unit test validated
the provider constraint, so it survived until the first live call.
"""

BANNED_TOP_LEVEL = ("oneOf", "anyOf", "allOf", "enum", "const", "not")


def assert_wire_clean(schema, owner):
    assert isinstance(schema, dict) and schema.get("type") == "object", owner
    banned = [key for key in BANNED_TOP_LEVEL if key in schema]
    assert not banned, f"{owner} wire schema carries banned top-level keys: {banned}"


def test_project_facade_schema_is_wire_clean():
    from sag.tools.project_tool import ProjectTool

    assert_wire_clean(ProjectTool().get_parameter_schema(), "project")


def test_project_env_branch_still_activates_without_a_schema_default():
    """The default the allOf used to state lives in execute: absent activate
    is forced True, explicit False is refused with the registered guidance."""
    from sag.tools.project_tool import ProjectTool

    class RecordingEnv:
        def __init__(self):
            self.calls = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            from sag.tools.base import ToolResult

            return ToolResult.completed_success(output="ok")

    env = RecordingEnv()
    tool = ProjectTool(env_tool=env)
    tool.execute(action="env", tool="maven", executable="/usr/bin/mvn")

    assert env.calls and env.calls[0]["activate"] is True
