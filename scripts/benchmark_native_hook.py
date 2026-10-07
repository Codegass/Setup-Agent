#!/usr/bin/env python3
"""Silent, fail-open transport for native harness observation hooks.

Runs only in an experiment container. Never executes the observed command,
edits tool arguments/results, or emits a decision or model-visible context.
Evidence collection failure is recorded by the host; it must not block tools.
"""

import json
import os
import sys
import urllib.request

# Only launcher inputs needed by the existing physical verifier. Never forward
# API keys, gateway credentials, or the rest of the hook process environment.
ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "JAVA_HOME",
    "MAVEN_HOME",
    "M2_HOME",
    "MAVEN_ARGS",
    "MAVEN_OPTS",
    "MAVEN_CONFIG",
    "MAVEN_PROJECTBASEDIR",
    "MAVEN_SKIP_RC",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "JAVA_OPTS",
    "GRADLE_OPTS",
    "GRADLE_USER_HOME",
)


def main():
    try:
        event = json.load(sys.stdin)
        event["observer_environment"] = {
            k: os.environ[k] for k in ENVIRONMENT_KEYS if k in os.environ
        }
        data = json.dumps(event).encode()
        request = urllib.request.Request(
            os.environ["BENCH_OBSERVER_URL"] + "/event",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + os.environ["BENCH_OBSERVER_TOKEN"],
            },
        )
        # A missing/late observation cannot be used to certify the execution.
        with urllib.request.urlopen(request, timeout=180) as response:
            response.read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
