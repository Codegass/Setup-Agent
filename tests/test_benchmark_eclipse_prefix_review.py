import base64
import hashlib

from scripts.benchmark_eclipse_prefix_review import direct_test_candidate, review


class FakeCollector:
    def __init__(self,payloads):self.payloads=iter(payloads)
    def get(self,url):return {'url':url,'data':next(self.payloads)}
    def json(self,response):return response['data']


ROW={'repo':'a/b','repository_id':1,'default_branch':'main','default_branch_sha':'population-sha'}
RUN={'id':2,'run_attempt':1,'head_sha':'run-sha','head_branch':'main','event':'push','name':'CI',
     'path':'.github/workflows/ci.yml','conclusion':'failure','html_url':'https://github.com/a/b/actions/runs/2',
     'created_at':'2026-09-22T00:00:00Z','updated_at':'2026-09-22T00:20:00Z'}


def source(text):
    raw=text.encode()
    return {'type':'file','encoding':'base64','content':base64.b64encode(raw).decode(),
            'sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}


def test_failed_cell_is_retained_and_partial_search_not_canonical():
    job={'id':3,'run_id':2,'run_attempt':1,'head_sha':'run-sha','name':'build','labels':['ubuntu-latest'],
         'status':'completed','completed_at':'2026-09-22T00:10:00Z','conclusion':'failure',
         'html_url':'https://github.com/a/b/actions/runs/2/job/3','steps':[{'name':'Build'}]}
    config=source('jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Build\n        run: mvn verify\n')
    collector=FakeCollector([{'total_count':200,'workflow_runs':[RUN]},config,{'total_count':1,'jobs':[job]},None])
    result=review(ROW,collector)
    assert result['selected_candidate']['sha']=='run-sha'
    assert result['selected_candidate']['conclusion']=='failure'
    assert result['selected_candidate']['admitted'] is False
    assert result['search_extent']['all_pages_scanned'] is False


def test_default_goal_is_unknown_not_no_task():
    config=source('jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: mvn -B\n')
    result=review(ROW,FakeCollector([{'total_count':1,'workflow_runs':[RUN]},config]))
    assert result['selected_candidate'] is None
    assert 'defaultGoal' in result['inspected_runs'][0]['gap']


def test_different_attempt_refuses_log_capture():
    job={'id':3,'run_id':2,'run_attempt':2,'head_sha':'run-sha'}
    config=source('jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: mvn verify\n')
    result=review(ROW,FakeCollector([{'total_count':1,'workflow_runs':[RUN]},config,{'total_count':1,'jobs':[job]}]))
    assert result['selected_candidate'] is None
    assert 'differs' in result['inspected_runs'][0]['gap']


def test_verify_with_explicit_test_skip_not_test_candidate():
    base={'contains_build_tool':True,'contains_test_lifecycle_token':True}
    assert not direct_test_candidate({**base,'command':'mvn verify checkstyle:check -DskipTests=true'})
    assert not direct_test_candidate({**base,'command':'mvn verify -DskipTests'})
    assert direct_test_candidate({**base,'command':'mvn verify -DskipTests=false'})
    assert direct_test_candidate({**base,'command':'mvn verify -DskipTests=true -DskipTests=false'})
    assert not direct_test_candidate({**base,'command':'mvn verify -DskipTests=false -DskipTests=true'})
    assert direct_test_candidate({**base,'command':'mvn verify -DskipTests=${SKIP}'})
    assert direct_test_candidate({**base,'command':'mvn verify -DskipITs=true'})
    assert not direct_test_candidate({**base,'command':'gradle check -x test'})
