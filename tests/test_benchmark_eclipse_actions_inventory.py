from scripts.benchmark_eclipse_actions_inventory import workflow_index
from scripts.benchmark_eclipse_ci_inventory import inventory


class FakeCollector:
    def __init__(self,pages):self.pages=iter(pages)
    def get(self,url):return next(self.pages)
    def json(self,value):return value['data']


def test_workflow_pagination_must_close_exact_total():
    prefix='https://api.github.com/repos/a/b/actions/workflows?'
    pages=[{'data':{'total_count':2,'workflows':[{'id':1}]},'response_headers':{'link':f'<{prefix}page=2>; rel="next"'}},
           {'data':{'total_count':2,'workflows':[{'id':2}]}}]
    assert workflow_index('a/b',FakeCollector(pages))['pagination_complete']


def test_incomplete_index_not_no_workflows():
    result=workflow_index('a/b',FakeCollector([{'data':{'total_count':2,'workflows':[{'id':1}]}}]))
    assert result['status']=='unavailable'


def test_missing_directory_project_does_not_mean_no_ci():
    rows=inventory({'repositories':[{'repo':'a/b','repository_id':1,'new_candidate':True,
                    'project_ids':['unknown'],'population_status':'eligible',
                    'default_branch':'main','default_branch_sha':'abc'}]}, {'instances':[]})
    assert rows[0]['task_admitted'] is False
    assert 'review_pending' in rows[0]['status']
