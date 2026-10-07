"""A Maven exec goal cannot silently certify nested build and test work."""
import hashlib

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events


def assess(tmp_path, nested, *, prefix='', suffix='', terminal=True):
    text=(prefix+'[INFO] --- exec:3.1.0:exec (gradle) @ plugin ---\n'+nested+suffix
          +('[INFO] BUILD SUCCESS\n' if terminal else ''))
    log=tmp_path/'command.log';log.write_text(text)
    ref={'path':'command.log','sha256':hashlib.sha256(log.read_bytes()).hexdigest()}
    row={'id':'compile','module':'plugin','kind':'compile',
         'scope':{'status':'declared','module_path':'plugin'},
         'validation':{'rule':'native_goal','goals':['exec:exec'],
            'native_bindings':[{'goal':'exec:exec','execution':'gradle','occurrence':0,'position':0}],
            'nested_gradle_task':':compileJava'}}
    invocation={'runner':'maven','invocation_id':'one','status':'completed' if terminal else 'timeout',
                'exit_code':0 if terminal else -9,'log_complete':True,'log':ref}
    return native_requirement(row,invocation,tmp_path,text,maven_events(text,terminal=terminal,serial=True))


def test_nested_compile_requires_its_own_task_outcome(tmp_path):
    result=assess(tmp_path,'> Task :compileJava\n> Task :jar\n')
    assert result['status']=='passed' and result['started'] is True


@pytest.mark.parametrize('output,status,started',[
    ('','unavailable',None),
    ('> Task :other\n','unavailable',None),
    ('> Task :compileJava UP-TO-DATE\n','unavailable',False),
    ('> Task :compileJava NO-SOURCE\n','unavailable',False),
    ('> Task :compileJava FROM-CACHE\n','unavailable',False),
    ('> Task :compileJava SKIPPED\n','not_run',False),
    ('> Task :compileJava FAILED\n','failed',True),
    ('> Task :compileJava\n> Task :compileJava\n','unavailable',True),
])
def test_missing_skipped_or_reused_task_does_not_inherit_parent_success(tmp_path,output,status,started):
    result=assess(tmp_path,output)
    assert result['status']==status and result['started'] is started


def test_task_outside_exact_parent_is_not_evidence(tmp_path):
    assert assess(tmp_path,'',prefix='> Task :compileJava\n')['status']=='unavailable'
    assert assess(tmp_path,'',suffix='[INFO] --- exec:3.1.0:exec (other) @ plugin ---\n> Task :compileJava\n')['status']=='unavailable'


def test_interrupted_parent_does_not_certify_nested_completion(tmp_path):
    assert assess(tmp_path,'> Task :compileJava\n',terminal=False)['status']=='unavailable'
