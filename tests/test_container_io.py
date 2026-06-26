"""Shared container text writer: small heredoc fast path + base64 chunk path.

Regression coverage for "argument list too long": embedding a large payload in a
single shell command exceeds the kernel per-arg limit, so big build logs and
accumulated branch-history JSON must stream as chunks instead.
"""

import base64
import re

from sag.utils.container_io import DEFAULT_MAX_CMD_CHARS, write_container_text


class FakeContainer:
    """Minimal container FS supporting the writer's command shapes."""

    def __init__(self):
        self.commands = []
        self.files = {}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)

        if command.startswith("rm -f "):
            self.files.pop(command[len("rm -f ") :].split()[0], None)
            return {"exit_code": 0, "output": ""}

        m = re.match(r"printf '%s' '(.*)' >> (\S+)$", command, re.DOTALL)
        if m:
            chunk, target = m.group(1), m.group(2)
            self.files[target] = self.files.get(target, "") + chunk
            return {"exit_code": 0, "output": ""}

        m = re.match(r"base64 -d (\S+) (>>|>) (\S+) && printf '\\n' >> \S+ && rm -f \S+$", command)
        if m:
            tmp, operator, path = m.group(1), m.group(2), m.group(3)
            decoded = base64.b64decode(self.files.get(tmp, "")).decode("utf-8", "replace")
            payload = decoded + "\n"
            self.files[path] = (self.files.get(path, "") + payload) if operator == ">>" else payload
            self.files.pop(tmp, None)
            return {"exit_code": 0, "output": ""}

        # heredoc: cat <op> <path> <<'DELIM'\n<content>\nDELIM
        if command.startswith("cat >> ") or command.startswith("cat > "):
            operator = ">>" if command.startswith("cat >> ") else ">"
            path = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.files[path] = (
                (self.files.get(path, "") + payload + "\n") if operator == ">>" else payload + "\n"
            )
            return {"exit_code": 0, "output": ""}

        return {"exit_code": 0, "output": ""}


def test_small_content_uses_single_heredoc_and_writes():
    fake = FakeContainer()
    assert write_container_text(fake, "/c/f.json", '{"a": 1}')
    assert fake.files["/c/f.json"] == '{"a": 1}\n'
    # One write command, no chunking machinery.
    assert not any(cmd.startswith("printf '%s'") for cmd in fake.commands)


def test_large_content_streams_in_chunks_and_round_trips():
    fake = FakeContainer()
    big = '{"history": "' + ("x" * (DEFAULT_MAX_CMD_CHARS * 2)) + '"}'
    assert len(big) > DEFAULT_MAX_CMD_CHARS

    assert write_container_text(fake, "/c/branch.json", big)

    # Round-trips intact (minus the trailing newline the writer adds).
    assert fake.files["/c/branch.json"].rstrip("\n") == big
    # No single command exceeded the per-arg cap (the bug cause).
    assert max(len(cmd) for cmd in fake.commands) <= DEFAULT_MAX_CMD_CHARS + 200
    # It actually used the chunk path.
    assert any(cmd.startswith("printf '%s'") for cmd in fake.commands)


def test_append_mode_keeps_prior_content():
    fake = FakeContainer()
    write_container_text(fake, "/c/log.jsonl", '{"n": 1}', append=True)
    write_container_text(fake, "/c/log.jsonl", '{"n": 2}', append=True)
    assert fake.files["/c/log.jsonl"] == '{"n": 1}\n{"n": 2}\n'
