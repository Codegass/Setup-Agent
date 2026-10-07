import json
import subprocess
import sys
from pathlib import Path

from scripts.benchmark_opencode_entrypoint import terminal_event


def event(kind,reason=None,session='root'):
    return json.dumps({'type':kind,'sessionID':session,'part':{'reason':reason}})


def test_only_the_native_parent_terminal_event_closes_a_run():
    session,terminal=terminal_event(event('step_start'),None)
    assert session=='root' and terminal is None
    for line in [event('text','stop'),event('step_finish','tool-calls'),event('step_finish','stop','child'),'Done, all tests passed']:
        assert terminal_event(line,session)==('root',None)
    assert terminal_event(event('step_finish','stop'),session)[1]['type']=='step_finish'


def test_native_terminal_releases_hung_cli_without_a_false_exit_claim():
    script=Path(__file__).resolve().parents[1]/'scripts/benchmark_opencode_entrypoint.py'
    child='import time;print('+repr(event('step_start'))+',flush=True);print('+repr(event('step_finish','stop'))+',flush=True);time.sleep(120)'
    done=subprocess.run([sys.executable,str(script),'--',sys.executable,'-c',child],capture_output=True,text=True,timeout=5)
    last=json.loads(done.stdout.splitlines()[-1])
    assert done.returncode==0
    assert last['boundary']=='opencode_native_stop' and last['cli_exit_code']!=0


def test_final_prose_and_intermediate_steps_cannot_hide_a_process_failure():
    script=Path(__file__).resolve().parents[1]/'scripts/benchmark_opencode_entrypoint.py'
    child='print("All tests passed");raise SystemExit(42)'
    done=subprocess.run([sys.executable,str(script),'--',sys.executable,'-c',child],capture_output=True,text=True,timeout=5)
    assert done.returncode==42
    assert json.loads(done.stdout.splitlines()[-1])['boundary']=='process_exit'
