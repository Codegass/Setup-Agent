"""The baseline arms' process tracer watches the Maven launcher only.

Following children (`strace -ff`) put every Maven JVM thread and every surefire fork under ptrace, which stops each
traced thread on every system call; in the pinned image curator's TestCuratorCache.testGreaterThan64kZNodes then
failed 3 of 3 at 110-115 s against 13-15 s untraced (output/baselines-fifty-20261021/analysis/strace-slowdown/).
The observer reads only the launcher's own exit line, so the launcher alone is traced.
"""

from scripts.benchmark_native_capture import process_trace_argv
from scripts.benchmark_native_observer import process_trace_path
from sag.benchmark.native_hooks import process_terminal

TRACER = ["/opt/native-trace/usr/bin/strace"]


def test_tracer_does_not_follow_children_or_threads():
    argv = process_trace_argv(TRACER, 4242)
    assert not [a for a in argv if a in {"-f", "-ff", "--follow-forks", "--output-separately"}]


def test_tracer_attaches_to_the_launcher_and_records_process_calls():
    argv = process_trace_argv(TRACER, 4242)
    assert argv[: len(TRACER)] == TRACER
    assert argv[argv.index("-p") + 1] == "4242"
    assert argv[argv.index("-e") + 1] == "trace=process"
    assert "-ttt" in argv


def test_tracer_writes_the_file_the_observer_reads():
    argv = process_trace_argv(TRACER, 4242)
    assert argv[argv.index("-o") + 1] == process_trace_path(4242) == "/opt/benchmark-process-traces/process.4242"


def test_launcher_only_trace_has_the_one_exit_line_the_observer_needs():
    # Shape of the launcher-only trace from the attach test (no pid prefixes, one exit line).
    raw = (
        b"1792884712.101 execve(\"./mvnw\", [\"./mvnw\", \"-B\"], 0x7ffd /* 12 vars */) = 0\n"
        b"1792884713.554 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 11980\n"
        b"1792884735.459 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=11980, si_status=0} ---\n"
        b"1792884735.460377 +++ exited with 0 +++\n"
    )
    assert process_terminal(raw) == {"exit_epoch": 1792884735.460377, "exit_code": 0}
