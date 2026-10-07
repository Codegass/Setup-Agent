"""Docker transport/configuration for passive native tool hooks."""

import json
import shlex
import subprocess
import time
from pathlib import Path

from scripts.benchmark_native_observer import POLICY, NativeObserver, ObserverServer

CLIENT_VERSIONS = {"claude": "2.1.267 (Claude Code)", "opencode": "1.18.32"}


def claude_settings():
    return {
        "hooks": {
            event: [
                {
                    "matcher": "Bash|TaskOutput",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /opt/benchmark-native-hook.py",
                            "timeout": 180,
                        }
                    ],
                }
            ]
            for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
        }
    }


class DockerCapture:
    def __init__(self, *, container, arm, output, run_id, checkout, task, spec, port):
        self.container = container
        architecture = subprocess.check_output(
            ["docker", "info", "--format", "{{.Architecture}}"], text=True
        ).strip()
        tracer = (
            [
                "/opt/native-trace/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
                "--library-path",
                "/opt/native-trace/usr/lib/aarch64-linux-gnu",
                "/opt/native-trace/usr/bin/strace",
            ]
            if architecture in {"aarch64", "arm64"}
            else ["strace"]
        )
        subprocess.run(
            ["docker", "exec", container, *tracer, "--version"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        subprocess.run(
            ["docker", "exec", container, "mkdir", "-p", "/opt/benchmark-process-traces"],
            check=True,
            capture_output=True,
        )

        def start_process_trace(pid):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-d",
                    container,
                    *tracer,
                    "-ff",
                    "-ttt",
                    "-s",
                    "4096",
                    "-e",
                    "trace=process",
                    "-o",
                    "/opt/benchmark-process-traces/process",
                    "-p",
                    str(pid),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            # Read the kernel's actual attach state before allowing the Maven
            # launcher to continue. This never traces the native LLM client.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = subprocess.check_output(
                    ["docker", "exec", container, "cat", f"/proc/{pid}/status"],
                    text=True,
                    timeout=10,
                )
                if any(
                    line.startswith("TracerPid:") and int(line.split()[1]) > 0
                    for line in status.splitlines()
                ):
                    return
                time.sleep(0.05)
            raise ValueError("Native Maven process tracer did not attach")

        def execute(command, **kwargs):
            result = subprocess.run(
                ["docker", "exec", container, *shlex.split(command)],
                capture_output=True,
                timeout=kwargs.get("timeout", 120),
            )
            return {
                "success": result.returncode == 0,
                "exit_code": result.returncode,
                "output": result.stdout.decode(),
                "stderr": result.stderr.decode(),
            }

        def read_file(path):
            program = "import pathlib,sys,stat; p=pathlib.Path(sys.argv[1]); assert p.is_absolute() and not any(q.is_symlink() for q in [p,*p.parents]); a=p.stat(); assert stat.S_ISREG(a.st_mode); data=p.read_bytes(); b=p.stat(); assert (a.st_size,a.st_mtime_ns,a.st_ino)==(b.st_size,b.st_mtime_ns,b.st_ino); sys.stdout.buffer.write(data)"
            return subprocess.check_output(
                ["docker", "exec", container, "python3", "-c", program, path], timeout=120
            )

        def list_traces():
            program = "import pathlib,json; p=pathlib.Path('/opt/sag-maven-observations'); print(json.dumps([d.name for d in p.iterdir()] if p.exists() else []))"
            return json.loads(
                subprocess.check_output(
                    ["docker", "exec", container, "python3", "-c", program], timeout=30
                )
            )

        def copy_trace(name, destination):
            if destination.exists():
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "docker",
                    "cp",
                    container + ":/opt/sag-maven-observations/" + name,
                    str(destination),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )

        self.observer = NativeObserver(
            base=Path(output) / "agent-records",
            arm=arm,
            run_id=run_id,
            root=checkout,
            task=task,
            spec=spec,
            execute=execute,
            read_file=read_file,
            list_traces=list_traces,
            copy_trace=copy_trace,
            start_process_trace=start_process_trace,
        )
        self.server = ObserverServer(self.observer, port)
        self.environment = {
            "BENCH_OBSERVER_URL": f"http://host.docker.internal:{port}",
            "BENCH_OBSERVER_TOKEN": self.server.token,
        }

    def close(self, transcript):
        # Seal first. Late/background notifications cannot improve primary data.
        try:
            return self.observer.close(transcript)
        finally:
            self.server.close()

    def stop(self):
        self.server.close()
