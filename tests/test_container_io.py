"""Shared container text writer: small heredoc fast path + base64 chunk path.

Regression coverage for "argument list too long": embedding a large payload in a
single shell command exceeds the kernel per-arg limit, so big build logs and
accumulated branch-history JSON must stream as chunks instead.
"""

import base64
import hashlib
import json
import re
import shlex
import subprocess
import sys

import pytest

from sag.runtime.container_io import ContainerFileReadError, read_container_prefix
from sag.utils.container_io import (
    DEFAULT_MAX_CMD_CHARS,
    ContainerWriteResult,
    compare_publish_container_text_atomic,
    write_container_text,
    write_container_text_atomic,
)


class FakeContainer:
    """Minimal container FS supporting the writer's command shapes."""

    def __init__(self, *, fail_on=None, corrupt_decoded=False):
        self.commands = []
        self.files = {}
        self.fail_on = fail_on
        self.corrupt_decoded = corrupt_decoded

    def execute_command(self, command, **kwargs):
        self.commands.append(command)

        if self.fail_on and self.fail_on in command:
            return {"exit_code": 1, "output": "injected failure"}

        tokens = shlex.split(command)

        if tokens[:3] == ["mkdir", "-p", "--"]:
            return {"exit_code": 0, "output": ""}

        if tokens[:2] == ["rm", "-f"]:
            targets = tokens[3:] if tokens[2:3] == ["--"] else tokens[2:]
            for target in targets:
                self.files.pop(target, None)
            return {"exit_code": 0, "output": ""}

        if len(tokens) == 3 and tokens[:2] == [":", ">"]:
            self.files[tokens[2]] = ""
            return {"exit_code": 0, "output": ""}

        if len(tokens) == 5 and tokens[:2] == ["printf", "%s"] and tokens[3] == ">>":
            chunk, target = tokens[2], tokens[4]
            self.files[target] = self.files.get(target, "") + chunk
            return {"exit_code": 0, "output": ""}

        if tokens[:2] == ["base64", "--decode"] and tokens[-2:-1] == [">"]:
            source, target = tokens[2], tokens[-1]
            decoded = base64.b64decode(self.files.get(source, "")).decode("utf-8")
            self.files[target] = decoded + ("!" if self.corrupt_decoded else "")
            return {"exit_code": 0, "output": ""}

        if (
            tokens[:2] == ["python3", "-c"]
            and "hashlib.sha256" in tokens[2]
            and "fcntl.flock" not in tokens[2]
        ):
            path, expected_bytes, expected_sha = tokens[3:6]
            payload = self.files.get(path, "").encode("utf-8")
            valid = (
                len(payload) == int(expected_bytes)
                and hashlib.sha256(payload).hexdigest() == expected_sha
            )
            return {"exit_code": 0 if valid else 1, "output": ""}

        if tokens[:2] == ["python3", "-c"] and "json.load" in tokens[2]:
            try:
                json.loads(self.files.get(tokens[3], ""))
            except (TypeError, json.JSONDecodeError):
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": ""}

        if tokens[:2] == ["python3", "-c"] and "fcntl.flock" in tokens[2]:
            target, candidate, _lock_path, expected, expected_bytes, expected_sha = tokens[3:9]
            candidate_bytes = self.files.get(candidate, "").encode("utf-8")
            if (
                len(candidate_bytes) != int(expected_bytes)
                or hashlib.sha256(candidate_bytes).hexdigest() != expected_sha
            ):
                return {"exit_code": 76, "output": "SAG_CAS_CANDIDATE_INVALID\n"}
            current = self.files.get(target)
            actual = (
                "absent"
                if current is None
                else "sha256:" + hashlib.sha256(current.encode("utf-8")).hexdigest()
            )
            if actual != expected:
                return {"exit_code": 75, "output": "SAG_CAS_CONFLICT\n"}
            self.files[target] = self.files.pop(candidate)
            return {"exit_code": 0, "output": ""}

        if tokens[:3] == ["mv", "-f", "--"]:
            source, target = tokens[3:5]
            self.files[target] = self.files.pop(source)
            return {"exit_code": 0, "output": ""}

        if (len(tokens) == 2 and tokens[0] == "cat") or (
            len(tokens) == 3 and tokens[:2] == ["cat", "--"]
        ):
            path = tokens[-1]
            if path not in self.files:
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": self.files[path]}

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

    def execute_control_command(self, command, **kwargs):
        return self.execute_command(command, **kwargs)


def test_fake_container_single_file_cat_preserves_exact_existing_body():
    fake = FakeContainer()
    fake.files["/c/evidence.json"] = '{"a":1}\n'

    assert fake.execute_command("cat /c/evidence.json") == {
        "exit_code": 0,
        "output": '{"a":1}\n',
    }
    assert fake.execute_command("cat -- /c/missing.json")["exit_code"] == 1


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


class CountingText(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.encode_calls = 0
        return instance

    def encode(self, *args, **kwargs):
        self.encode_calls += 1
        return super().encode(*args, **kwargs)


@pytest.mark.parametrize("use_callback", [False, True], ids=["orchestrator", "callback"])
def test_atomic_writer_round_trips_exact_bytes_with_typed_result(use_callback):
    fake = FakeContainer()
    content = CountingText('{"history":"' + ("café" * 20000) + '"}')
    execute = fake.execute_command if use_callback else fake

    result = write_container_text_atomic(
        execute,
        "/c/evidence.json",
        content,
        validate_json=True,
    )

    payload = str(content).encode("utf-8")
    assert result == ContainerWriteResult(
        persisted=True,
        code="persisted",
        bytes_written=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert fake.files["/c/evidence.json"] == content
    assert content.encode_calls == 1
    assert not any(path.endswith(".tmp") for path in fake.files)


def test_atomic_writer_bounds_every_command_and_uses_unique_temp_names():
    fake = FakeContainer()
    content = "x" * (DEFAULT_MAX_CMD_CHARS * 3)

    first = write_container_text_atomic(fake, "/c/large output.txt", content)
    first_temp = next(
        shlex.split(command)[2]
        for command in fake.commands
        if shlex.split(command)[:2] == [":", ">"]
    )
    command_boundary = len(fake.commands)
    second = write_container_text_atomic(fake, "/c/large output.txt", content)
    second_temp = next(
        shlex.split(command)[2]
        for command in fake.commands[command_boundary:]
        if shlex.split(command)[:2] == [":", ">"]
    )

    assert first.persisted and second.persisted
    assert first_temp != second_temp
    assert max(map(len, fake.commands)) <= 60200
    assert fake.files["/c/large output.txt"] == content


@pytest.mark.parametrize(
    ("failure_marker", "expected_code"),
    [
        ("printf '%s'", "transport_write_failed"),
        ("base64 --decode", "transport_write_failed"),
        ("mv -f --", "transport_publish_failed"),
    ],
)
def test_atomic_writer_cleans_temps_and_preserves_final_on_command_failure(
    failure_marker, expected_code
):
    final = "/c/result.json"
    fake = FakeContainer(fail_on=failure_marker)
    fake.files[final] = "old-complete-value"

    result = write_container_text_atomic(fake, final, '{"new":true}', validate_json=True)

    assert result == ContainerWriteResult(False, expected_code)
    assert fake.files[final] == "old-complete-value"
    assert not any(path != final and path.endswith(".tmp") for path in fake.files)
    assert any(command.startswith("rm -f -- ") for command in fake.commands)


def test_atomic_writer_rejects_hash_mismatch_before_publish_and_cleans_temps():
    final = "/c/result.json"
    fake = FakeContainer(corrupt_decoded=True)
    fake.files[final] = "old-complete-value"

    result = write_container_text_atomic(fake, final, '{"new":true}', validate_json=True)

    assert result == ContainerWriteResult(False, "transport_validation_failed")
    assert fake.files[final] == "old-complete-value"
    assert not any(path != final and path.endswith(".tmp") for path in fake.files)


def test_atomic_writer_validates_json_in_container_without_returning_body():
    final = "/c/result.json"
    fake = FakeContainer()
    fake.files[final] = '{"old":true}'

    result = write_container_text_atomic(fake, final, "{not-json", validate_json=True)

    assert result == ContainerWriteResult(False, "transport_validation_failed")
    assert fake.files[final] == '{"old":true}'
    json_command = next(command for command in fake.commands if "json.load" in command)
    assert "{not-json" not in json_command
    assert not any(path != final and path.endswith(".tmp") for path in fake.files)


def test_atomic_writer_shell_quotes_final_and_temp_paths():
    final = "/c/odd name'; touch /c/pwn; '.json"
    fake = FakeContainer()

    result = write_container_text_atomic(fake, final, "safe")

    assert result.persisted
    assert fake.files[final] == "safe"
    assert "/c/pwn" not in fake.files


def test_atomic_writer_rejects_a_contradictory_success_flag_on_nonzero_exit():
    final = "/c/result.json"

    class ContradictoryPublish(FakeContainer):
        def execute_command(self, command, **kwargs):
            if command.startswith("mv -f --"):
                self.commands.append(command)
                return {"exit_code": 1, "success": True, "output": "mv failed"}
            return super().execute_command(command, **kwargs)

    fake = ContradictoryPublish()
    fake.files[final] = "old-complete-value"

    result = write_container_text_atomic(fake, final, '{"new":true}', validate_json=True)

    assert result == ContainerWriteResult(False, "transport_publish_failed")
    assert fake.files[final] == "old-complete-value"


def test_compare_publish_replaces_only_the_exact_body_the_caller_read():
    final = "/c/result.json"
    fake = FakeContainer()
    fake.files[final] = '{"old":true}'

    result = compare_publish_container_text_atomic(
        fake,
        final,
        '{"new":true}',
        expected_content='{"old":true}',
        validate_json=True,
    )

    assert result.persisted is True
    assert fake.files[final] == '{"new":true}'
    assert not any(".candidate." in path for path in fake.files)


def test_compare_publish_reports_a_stale_read_without_overwriting_or_leaking_candidate():
    final = "/c/result.json"
    fake = FakeContainer()
    fake.files[final] = '{"newer":true}'

    result = compare_publish_container_text_atomic(
        fake,
        final,
        '{"stale":true}',
        expected_content='{"old":true}',
        validate_json=True,
    )

    assert result == ContainerWriteResult(False, "compare_conflict")
    assert fake.files[final] == '{"newer":true}'
    assert not any(".candidate." in path for path in fake.files)


def test_compare_publish_can_condition_on_verified_absence():
    final = "/c/result.json"
    fake = FakeContainer()

    result = compare_publish_container_text_atomic(
        fake,
        final,
        '{"first":true}',
        expected_content=None,
        validate_json=True,
    )

    assert result.persisted is True
    assert fake.files[final] == '{"first":true}'


def test_compare_publish_revalidates_the_staged_candidate_under_the_lock():
    final = "/c/result.json"

    class CandidateTamper(FakeContainer):
        def execute_command(self, command, **kwargs):
            tokens = shlex.split(command)
            if tokens[:2] == ["python3", "-c"] and "fcntl.flock" in tokens[2]:
                self.files[tokens[4]] += "!"
            return super().execute_command(command, **kwargs)

    fake = CandidateTamper()
    fake.files[final] = '{"old":true}'

    result = compare_publish_container_text_atomic(
        fake,
        final,
        '{"new":true}',
        expected_content='{"old":true}',
        validate_json=True,
    )

    assert result == ContainerWriteResult(False, "transport_publish_failed")
    assert fake.files[final] == '{"old":true}'
    assert not any(".candidate." in path for path in fake.files)


def test_atomic_writer_prefers_clean_control_executor():
    class ControlContainer(FakeContainer):
        def __init__(self):
            super().__init__()
            self.normal_commands = []
            self.control_commands = []

        def execute_command(self, command, **kwargs):
            self.normal_commands.append(command)
            return {"success": False, "exit_code": 97, "output": "poisoned overlay"}

        def execute_control_command(self, command, **kwargs):
            self.control_commands.append(command)
            return super().execute_command(command, **kwargs)

    fake = ControlContainer()

    result = write_container_text_atomic(
        fake,
        "/c/strict.json",
        '{"strict":true}',
        validate_json=True,
    )

    assert result.persisted is True
    assert fake.files["/c/strict.json"] == '{"strict":true}'
    assert fake.control_commands
    assert fake.normal_commands == []


def test_bound_execute_callback_uses_its_owner_clean_control_executor():
    class ControlContainer(FakeContainer):
        def __init__(self):
            super().__init__()
            self.normal_commands = []
            self.control_commands = []

        def execute_command(self, command, **kwargs):
            self.normal_commands.append(command)
            return {"success": False, "exit_code": 97, "output": "poisoned overlay"}

        def execute_control_command(self, command, **kwargs):
            self.control_commands.append(command)
            return super().execute_command(command, **kwargs)

    fake = ControlContainer()

    result = write_container_text_atomic(
        fake.execute_command,
        "/c/callback.json",
        '{"strict":true}',
        validate_json=True,
    )

    assert result.persisted is True
    assert fake.files["/c/callback.json"] == '{"strict":true}'
    assert fake.control_commands
    assert fake.normal_commands == []


def test_bound_normal_callback_without_a_clean_owner_fails_closed():
    class UnsafeOwner:
        def __init__(self):
            self.normal_commands = []

        def execute_command(self, command, **_kwargs):
            self.normal_commands.append(command)
            return {"success": True, "exit_code": 0, "output": ""}

    owner = UnsafeOwner()

    result = write_container_text_atomic(
        owner.execute_command,
        "/c/must-not-use-runtime-overlay.json",
        '{"strict":true}',
        validate_json=True,
    )

    assert result.persisted is False
    assert result.code == "invalid_arguments"
    assert owner.normal_commands == []


def _local_prefix_executor(command, **kwargs):
    """Run only the generated Python read, with production whitespace stripping."""
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    assert kwargs == {"truncate_output": False}
    completed = subprocess.run(
        [sys.executable, *argv[1:]], capture_output=True, check=False, timeout=10
    )
    return {
        "success": completed.returncode == 0,
        "exit_code": completed.returncode,
        "output": completed.stdout.decode("utf-8").strip(),
    }


@pytest.mark.parametrize(
    ("raw", "budget"),
    [
        pytest.param(b"", 0, id="empty-zero-budget"),
        pytest.param(b"", 8, id="empty-file"),
        pytest.param(b"abc", 0, id="nonempty-zero-budget"),
        pytest.param(b"abc", 3, id="exact-budget"),
        pytest.param(b"abcd", 3, id="over-budget"),
        pytest.param(" \n中文 café\n\n".encode("utf-8"), 100, id="utf8-and-whitespace"),
        pytest.param("a中\n".encode("utf-8"), 2, id="partial-utf8-codepoint"),
        pytest.param(b"a\x00\xff\n", 8, id="raw-binary-bytes"),
    ],
)
def test_prefix_transport_preserves_exact_bounded_bytes(tmp_path, raw, budget):
    path = tmp_path / "odd name ' ; $(touch forbidden)"
    path.write_bytes(raw)

    assert read_container_prefix(_local_prefix_executor, str(path), max_bytes=budget) == (
        raw[:budget],
        len(raw) > budget,
    )
    assert sorted(child.name for child in tmp_path.iterdir()) == [path.name]


def test_prefix_transport_reads_only_budget_plus_one_from_the_file(tmp_path, monkeypatch):
    import builtins
    import io
    from contextlib import redirect_stdout

    path = tmp_path / "large-document.txt"
    body = b"recipe\n" * 10_000
    path.write_bytes(body)
    read_sizes = []
    actual_open = builtins.open

    class BoundedReader:
        def __enter__(self):
            self.file = actual_open(path, "rb")
            return self

        def read(self, size=-1):
            assert size >= 0
            read_sizes.append(size)
            assert sum(read_sizes) <= 18
            return self.file.read(size)

        def __exit__(self, *_args):
            self.file.close()

    def guarded_open(requested_path, mode):
        assert requested_path == str(path)
        assert mode == "rb"
        return BoundedReader()

    def bounded_execute(command, **kwargs):
        argv = shlex.split(command)
        assert argv[:2] == ["python3", "-c"]
        assert kwargs == {"truncate_output": False}
        captured = io.StringIO()
        with monkeypatch.context() as scoped, redirect_stdout(captured):
            scoped.setattr(builtins, "open", guarded_open)
            scoped.setattr(sys, "argv", ["-c", *argv[3:]])
            exec(compile(argv[2], "<container-prefix-command>", "exec"), {})
        return {"success": True, "exit_code": 0, "output": captured.getvalue().strip()}

    assert read_container_prefix(bounded_execute, str(path), max_bytes=17) == (
        body[:17],
        True,
    )
    assert read_sizes == [18]


@pytest.mark.parametrize("budget", [-1, True, False, 1.5, "4", None])
def test_prefix_transport_rejects_noninteger_or_negative_budget_before_execution(budget):
    def forbidden_execute(*args, **kwargs):
        pytest.fail("an invalid budget must not execute")

    with pytest.raises(ValueError, match="nonnegative integer"):
        read_container_prefix(forbidden_execute, "/irrelevant", max_bytes=budget)


def _prefix_frame(raw=b"data\n", *, has_more=False):
    return (
        "SAG_FILE_PREFIX_V1\t"
        f"{len(raw)}\t{hashlib.sha256(raw).hexdigest()}\t{int(has_more)}\t"
        f"{base64.b64encode(raw).decode('ascii')}\nSAG_FILE_PREFIX_END_V1\n"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda frame: frame.split("\n")[0], id="missing-footer"),
        pytest.param(lambda frame: frame + "extra", id="extra-suffix"),
        pytest.param(lambda frame: "noise\n" + frame, id="extra-prefix"),
        pytest.param(lambda frame: frame.replace("V1", "V2", 1), id="wrong-marker"),
        pytest.param(lambda frame: frame.replace("\t5\t", "\t4\t", 1), id="wrong-length"),
        pytest.param(lambda frame: frame.replace("\t5\t", "\t05\t", 1), id="padded-length"),
        pytest.param(lambda frame: frame.replace("\t5\t", "\t99999\t", 1), id="large-length"),
        pytest.param(
            lambda frame: frame.replace(hashlib.sha256(b"data\n").hexdigest(), "0" * 64),
            id="wrong-digest",
        ),
        pytest.param(lambda frame: frame.replace("ZGF0YQo=", "!GF0YQo="), id="invalid-base64"),
        pytest.param(lambda frame: frame.replace("ZGF0YQo=", "ZGF0YQo=="), id="excess-padding"),
        pytest.param(lambda frame: frame.replace("ZGF0YQo=", "ZGF0YQp="), id="noncanonical-bits"),
        pytest.param(lambda frame: frame.replace("\t0\t", "\ttrue\t", 1), id="invalid-eof"),
        pytest.param(lambda frame: frame.replace("\t0\t", "\t1\t", 1), id="short-truncated"),
        pytest.param(lambda frame: None, id="non-text"),
    ],
)
def test_prefix_transport_rejects_incomplete_or_corrupt_frames(mutate):
    def execute(command, **kwargs):
        return {"success": True, "exit_code": 0, "output": mutate(_prefix_frame())}

    with pytest.raises(ContainerFileReadError):
        read_container_prefix(execute, "/workspace/README", max_bytes=8)


@pytest.mark.parametrize(
    "status",
    [
        {"success": False, "exit_code": 0},
        {"success": True, "exit_code": 1},
        {"success": True, "exit_code": 0, "dispatch_status": "container_unavailable"},
        {"success": True, "exit_code": False},
        {"success": True},
    ],
)
def test_prefix_transport_does_not_accept_valid_frame_from_failed_read(status):
    def execute(command, **kwargs):
        return {**status, "output": _prefix_frame()}

    with pytest.raises(ContainerFileReadError, match="did not succeed"):
        read_container_prefix(execute, "/workspace/README", max_bytes=8)


def test_prefix_transport_missing_file_is_a_failed_read(tmp_path):
    with pytest.raises(ContainerFileReadError, match="did not succeed"):
        read_container_prefix(_local_prefix_executor, str(tmp_path / "absent"), max_bytes=8)


def test_prefix_transport_prefers_clean_control_and_has_no_direct_file_shortcut(tmp_path):
    path = tmp_path / "document"
    path.write_bytes(b"actual\n")

    class ControlSource:
        files = {str(path): "unverified shortcut"}

        def read_file(self, path):
            pytest.fail("prefix reads must exercise the bounded transport")

        def execute_command(self, command, **kwargs):
            pytest.fail("prefix reads must use the clean control executor")

        def execute_control_command(self, command, **kwargs):
            return _local_prefix_executor(command, **kwargs)

    source = ControlSource()
    assert read_container_prefix(source.execute_command, str(path), max_bytes=8) == (
        b"actual\n",
        False,
    )
