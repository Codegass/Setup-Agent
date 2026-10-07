"""Bound a one-turn OpenCode run by its native terminal event, not UI teardown.

The pinned CLI emits step_finish(reason=stop) after the model's final step.
Neither final prose nor tool-call step completions close this boundary. The
controller records the actual exit status and terminates only the CLI process;
it does not kill project services or erase any worktree files.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone


def terminal_event(line, session):
    try:
        event=json.loads(line)
    except ValueError:
        return session, None
    if event.get('type')=='step_start' and session is None:
        session=event.get('sessionID')
    if (session and event.get('sessionID')==session and event.get('type')=='step_finish'
            and event.get('part',{}).get('reason')=='stop'):
        return session,event
    return session,None


def run(argv):
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    session=None;terminal=None
    try:
        for line in process.stdout:
            sys.stdout.write(line);sys.stdout.flush()
            session,terminal=terminal_event(line,session)
            if terminal is not None:
                process.terminate()
                break
        try:code=process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill();code=process.wait(timeout=10)
        print(json.dumps({'type':'controller_terminal','at':datetime.now(timezone.utc).isoformat(),
                          'boundary':'opencode_native_stop' if terminal else 'process_exit',
                          'session_id':session,'native_event':terminal,'cli_exit_code':code}),flush=True)
        return 0 if terminal else code
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=10)


if __name__=='__main__':
    args=sys.argv[1:]
    if args and args[0]=='--':args=args[1:]
    if not args:raise SystemExit('Expected the pinned OpenCode CLI command')
    raise SystemExit(run(args))
