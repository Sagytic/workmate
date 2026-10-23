import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent_hub import (COL_AGENTS, COL_MISSIONS, COL_RUNS, execute_run,
                               _overdue, install_agent_hub, seed_agent_hub, validate_result)
from backend.store import KST, Store, now


class NoModel:
    configured = False
    key = ''

    def _complete(self, *args, **kwargs):
        raise AssertionError('API-free analysis must not call a model')


class HoldingPool:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def flush(self):
        for fn, args in self.jobs:
            fn(*args)
        self.jobs.clear()


@pytest.fixture
def hub(tmp_path):
    store = Store(tmp_path / 'hub.db')
    pool = ThreadPoolExecutor(max_workers=3)
    ctx = SimpleNamespace(store=store, planner=NoModel(), engine=SimpleNamespace(pool=pool), HOSTED=False)
    app = FastAPI()
    seed_agent_hub(store)
    install_agent_hub(app, ctx)
    with TestClient(app) as client:
        yield client, ctx
    pool.shutdown(wait=True)


def wait(client, identity):
    for _ in range(150):
        run = client.get('/api/agent-hub/runs/' + identity).json()
        if run['status'] not in ('queued', 'running'):
            return run
        time.sleep(.01)
    raise AssertionError('Run did not finish')


def run(client, kind='meeting', request='결정: 검수 범위를 확정합니다.'):
    response = client.post(f'/api/agent-hub/agents/hub_{kind}/run', json={'request': request})
    assert response.status_code == 200, response.text
    return wait(client, response.json()['id'])


def document(store, identity, project, body):
    return store.put('documents', {'id': identity, 'title': identity, 'project': project, 'body': body, 'updated_at': now()})


def task(store, identity, **values):
    return store.put('tasks', {'id': identity, 'title': identity, 'status': 'todo', 'project': 'ERP',
                              'owner': '', 'due_at': None, 'updated_at': now(), **values})


def test_four_profiles_and_explicit_meeting_actions_work_without_model(hub):
    client, ctx = hub
    state = client.get('/api/agent-hub').json()
    assert {a['kind'] for a in state['agents']} == {'meeting', 'decision', 'radar', 'reflection'}
    output = run(client, request='회의 기록\n결정: 월말 마감을 2단계로 진행\n액션: 정산표 확인 | 담당: 김서연 | 기한: 2026-10-28 | 우선: 높음\n액션: 승인 방식 협의\n기억: 선호 보고 형식 = 한 페이지 요약')
    assert output['status'] == 'completed' and output['mode'] == 'rules'
    assert all(s['status'] == 'completed' for s in output['steps'])
    assert len(output['result']['decisions']) == 1
    action = output['result']['actions'][0]
    assert (action['owner'], action['due_at'], action['priority']) == ('김서연', '2026-10-28', 'high')
    assert output['result']['actions'][1]['due_at'] is None
    assert output['verification']['matched'] == 4 and output['verification']['unresolved'] == 0
    assert not ctx.store.all('tasks') and not ctx.store.all('memories')
    assert output['artifacts'][0]['url'].endswith('/report.html')


def test_profile_validation_and_project_scope_never_include_other_data(hub):
    client, ctx = hub
    document(ctx.store, 'doc_erp', 'ERP', '결정: ERP 승인 기준')
    document(ctx.store, 'doc_fin', '금융', '결정: 외부 금융 비공개 기준')
    task(ctx.store, 'erp_task')
    task(ctx.store, 'finance_task', project='금융')
    ctx.store.memory('개인 선호', '허용하지 않은 기억')
    assert client.post('/api/agent-hub/agents', json={'name': 'x', 'kind': 'unknown'}).status_code == 400
    assert client.post('/api/agent-hub/agents', json={'name': ' ', 'kind': 'radar'}).status_code == 400
    assert client.post('/api/agent-hub/agents', json={'name': 'x', 'document_ids': ['missing']}).status_code == 404
    assert client.post('/api/agent-hub/agents', json={'name': 'x', 'project': 'ERP', 'document_ids': ['doc_fin']}).status_code == 400
    profile = client.post('/api/agent-hub/agents', json={'name': 'ERP 기준', 'kind': 'radar', 'project': 'ERP', 'document_ids': ['doc_erp']}).json()
    identity = client.post(f'/api/agent-hub/agents/{profile["id"]}/run', json={'request': 'ERP 진행 상황'}).json()['id']
    output = wait(client, identity)
    assert {s['id'] for s in output['sources']} == {'request', 'document:doc_erp', 'task:erp_task', 'metrics'}
    assert '비공개' not in json.dumps(output, ensure_ascii=False)
    assert output['metrics']['total'] == 1
    # Blank project + no selected documents does not silently read the library.
    meeting = run(client)
    assert {s['id'] for s in meeting['sources']} == {'request'}


def test_profile_and_source_snapshots_survive_edits_deletion_and_archiving(hub):
    client, ctx = hub
    holding = HoldingPool()
    ctx.engine.pool = holding
    document(ctx.store, 'doc', 'ERP', '결정: 기존 승인 절차')
    client.patch('/api/agent-hub/agents/hub_meeting', json={'project': 'ERP', 'document_ids': ['doc'], 'instructions': '기존 지침'})
    response = client.post('/api/agent-hub/agents/hub_meeting/run', json={'request': '원래 요청'})
    identity = response.json()['id']
    client.patch('/api/agent-hub/agents/hub_meeting', json={'instructions': '수정한 지침', 'name': '변경한 이름'})
    ctx.store.delete('documents', 'doc')
    client.delete('/api/agent-hub/agents/hub_meeting')
    holding.flush()
    output = wait(client, identity)
    assert output['agent_snapshot']['instructions'] == '기존 지침'
    assert output['agent_snapshot']['name'] == '회의 후속 정리'
    assert output['request'] == '원래 요청'
    assert output['result']['decisions'][0]['title'] == '기존 승인 절차'
    assert client.get(output['artifacts'][0]['url']).status_code == 200
    assert client.post('/api/agent-hub/agents/hub_meeting/run', json={'request': '새 요청'}).status_code == 404
    seed_agent_hub(ctx.store)
    assert 'hub_meeting' not in {a['id'] for a in client.get('/api/agent-hub').json()['agents']}


def test_unresolved_evidence_cannot_invent_owner_or_deadline():
    sources = [{'id': 'request', 'title': '요청', 'type': 'request', 'excerpt': '액션: 보고서 확인 | 담당: 이지우 | 기한: 2026-10-28'}]
    raw = {'summary': '보고서 확인', 'actions': [
        {'title': '원문 날짜에 시각 추가', 'owner': '이지우', 'due_at': '2026-10-28T18:00:00+09:00', 'evidence_id': 'request', 'evidence_quote': sources[0]['excerpt']},
        {'title': '존재하지 않는 자료', 'owner': '김미정', 'due_at': '2026-10-30', 'evidence_id': 'other-document', 'evidence_quote': '마감 2026-10-30'},
        {'title': '없는 인용', 'owner': '한도윤', 'due_at': '2026-10-28', 'evidence_id': 'request', 'evidence_quote': '담당: 한도윤 | 기한: 2026-10-28'}]}
    output, verification = validate_result(raw, sources)
    assert all(a['due_at'] is None for a in output['actions'])
    assert output['actions'][0]['owner'] == '이지우'
    assert all(not a['owner'] for a in output['actions'][1:])
    assert verification['unresolved'] == 2 and verification['removed_deadlines'] == 3
    assert all(a['evidence_id'] is None for a in output['actions'][1:])


def test_followup_import_is_idempotent_and_keeps_explicit_date_precision(hub):
    client, ctx = hub
    output = run(client, request='액션: 계약 검토 | 담당: 박민서 | 기한: 2026-10-29\n액션: 예산 확인')
    url = f'/api/agent-hub/runs/{output["id"]}/actions'
    first = client.post(url, json={'indices': [0, 0, 1]}).json()
    second = client.post(url, json={'indices': [0, 1]}).json()
    assert first['created'] == 2 and second['created'] == 0 and second['existing'] == 2
    assert len(ctx.store.all('tasks')) == 2
    assert first['tasks'][0]['due_at'] == '2026-10-29' and first['tasks'][1]['due_at'] is None
    assert first['tasks'][0]['status'] == 'todo' and first['tasks'][0]['source_run_id'] == output['id']
    assert client.post(url, json={'indices': [-1]}).status_code == 400
    assert client.post(url, json={'indices': [99]}).status_code == 400


def test_memory_import_is_explicit_idempotent_and_preserves_conflicting_values(hub):
    client, ctx = hub
    ctx.store.memory('기존 선호', '유지할 내용')
    output = run(client, request='기억: 회의 선호 = 오전 일정\n기억: 기존 선호 = 다른 내용')
    url = f'/api/agent-hub/runs/{output["id"]}/memory'
    assert len(ctx.store.all('memories')) == 1
    first = client.post(url, json={'indices': [0, 1]}).json()
    second = client.post(url, json={'indices': [0, 1]}).json()
    assert first['created'] == 1 and len(first['conflicts']) == 1
    assert second['created'] == 0 and second['existing'] == 1
    assert next(m for m in ctx.store.all('memories') if m['key'] == '기존 선호')['value'] == '유지할 내용'


def test_radar_and_reflection_count_actual_completion_dates_and_scope(hub):
    client, ctx = hub
    current = datetime.now(KST)
    task(ctx.store, 'overdue', due_at=(current - timedelta(days=1)).isoformat())
    task(ctx.store, 'waiting', status='waiting_input', owner='담당자')
    task(ctx.store, 'recent', status='completed', completed_at=(current - timedelta(days=2)).isoformat())
    task(ctx.store, 'old', status='completed', completed_at=(current - timedelta(days=30)).isoformat(), updated_at=now())
    task(ctx.store, 'unknown_date', status='completed')
    task(ctx.store, 'other', status='completed', project='금융', completed_at=now())
    client.patch('/api/agent-hub/agents/hub_radar', json={'project': 'ERP'})
    client.patch('/api/agent-hub/agents/hub_reflection', json={'project': 'ERP'})
    radar = run(client, kind='radar', request='현재 프로젝트를 점검해 주세요.')
    reflection = run(client, kind='reflection', request='이번 주를 돌아보고 싶어요.')
    assert radar['metrics']['total'] == 5 and radar['metrics']['overdue'] == 1 and radar['metrics']['waiting'] == 1
    assert {r['title'] for r in radar['result']['risks']} == {'overdue', 'waiting'}
    assert reflection['metrics']['completed_last_7_days'] == 1
    assert [i['title'] for i in reflection['result']['insights']] == ['recent']
    assert '1건' in reflection['result']['summary']


def test_date_only_deadline_is_not_overdue_until_following_day(hub):
    client, ctx = hub
    current = datetime.now(KST)
    today, yesterday = current.date().isoformat(), (current - timedelta(days=1)).date().isoformat()
    assert not _overdue(today, current)
    assert _overdue(yesterday, current)
    assert _overdue((current - timedelta(seconds=1)).isoformat(), current)
    task(ctx.store, 'due_today', due_at=today, owner='담당자')
    task(ctx.store, 'due_yesterday', due_at=yesterday, owner='담당자')
    task(ctx.store, 'due_moment_ago', due_at=(current - timedelta(seconds=1)).isoformat(), owner='담당자')
    output = run(client, kind='radar', request='기한 확인')
    assert output['metrics']['overdue'] == 2
    assert {r['title'] for r in output['result']['risks']} == {'due_yesterday', 'due_moment_ago'}


def test_model_gets_scoped_input_and_failed_response_is_not_completed(hub):
    client, ctx = hub
    observed = []
    def complete(instructions, payload, **kwargs):
        observed.append((instructions, deepcopy(payload)))
        return {'summary': '모델이 정리한 결정', 'decisions': [{'title': '실행 범위', 'detail': '합의 내용',
                'evidence_id': 'request', 'evidence_quote': '결정: 이번 주 범위 확정'}]}
    ctx.planner = SimpleNamespace(configured=True, key='secret-key', _complete=complete)
    output = run(client, request='결정: 이번 주 범위 확정')
    assert output['mode'] == 'llm' and output['verification']['matched'] == 1
    assert len(observed) == 1 and 'untrusted data' in observed[0][0]
    assert observed[0][1]['sources'][0]['id'] == 'request'
    assert 'secret-key' not in json.dumps(output)
    ctx.planner._complete = lambda *a, **kw: {'summary': 'bad', 'actions': 'not an array'}
    failed = run(client)
    assert failed['status'] == 'failed' and not failed['artifacts']
    assert any(s['status'] == 'failed' for s in failed['steps'])
    assert client.get(f'/api/agent-hub/runs/{failed["id"]}/report.html').status_code == 409


def test_unverified_memory_is_not_imported(hub):
    client, ctx = hub
    ctx.planner = SimpleNamespace(configured=True, key='', _complete=lambda *a, **kw: {
        'summary': '기억 후보', 'memory_suggestions': [{'key': '없는 선호', 'value': '없는 내용',
        'evidence_id': 'request', 'evidence_quote': '원문에 없는 인용'}]})
    output = run(client)
    response = client.post(f'/api/agent-hub/runs/{output["id"]}/memory', json={'indices': [0]})
    assert response.status_code == 400 and not ctx.store.all('memories')


def test_report_escapes_user_document_and_model_markup(hub):
    client, ctx = hub
    output = run(client, request='결정: <script>alert(1)</script>\n액션: <img src=x onerror=alert(1)>')
    response = client.get(output['artifacts'][0]['url'])
    assert response.status_code == 200
    assert '<script>' not in response.text and '<img src=' not in response.text
    assert '&lt;script&gt;' in response.text and 'style-src' in response.headers['content-security-policy']
    assert 'attachment' in response.headers['content-disposition']


def test_mission_runs_are_real_independent_children_and_bad_profile_starts_nothing(hub):
    client, ctx = hub
    holding = HoldingPool()
    ctx.engine.pool = holding
    before = len(ctx.store.all(COL_RUNS))
    invalid = client.post('/api/agent-hub/missions', json={'request': '합동 검토', 'agent_ids': ['hub_meeting', 'missing']})
    assert invalid.status_code == 404 and len(ctx.store.all(COL_RUNS)) == before
    assert not ctx.store.all(COL_MISSIONS)
    response = client.post('/api/agent-hub/missions', json={'name': '출시 준비', 'request': '결정: 출시 범위 확인\n대안: 단계별 오픈 | 장점: 위험 분산 | 비용: 운영 2회',
                                                          'agent_ids': ['hub_meeting', 'hub_decision', 'hub_radar']})
    mission = response.json()
    assert mission['status'] == 'queued' and len(holding.jobs) == 3
    assert len(mission['run_ids']) == 3 and all(r['mission_id'] == mission['id'] for r in ctx.store.all(COL_RUNS))
    holding.flush()
    finished = client.get('/api/agent-hub/missions/' + mission['id']).json()
    assert finished['status'] == 'completed' and finished['completed_count'] == 3
    outputs = [client.get('/api/agent-hub/runs/' + identity).json() for identity in mission['run_ids']]
    assert {r['agent_snapshot']['kind'] for r in outputs} == {'meeting', 'decision', 'radar'}
    assert all(len(r['events']) == 7 for r in outputs)
    assert client.get('/api/agent-hub').json()['missions'][0]['id'] == mission['id']


def test_hosted_mission_capacity_is_atomic_across_other_jobs(hub):
    client, ctx = hub
    ctx.HOSTED = True
    ctx.engine.pool = HoldingPool()
    task(ctx.store, 'running-task', status='running')
    ctx.store.put('uat_runs', {'id': 'queued-uat', 'status': 'queued'})
    response = client.post('/api/agent-hub/missions', json={'request': '함께 검토', 'agent_ids': ['hub_meeting', 'hub_decision', 'hub_radar']})
    assert response.status_code == 429 and not ctx.store.all(COL_RUNS) and not ctx.store.all(COL_MISSIONS)
    response = client.post('/api/agent-hub/missions', json={'request': '함께 검토', 'agent_ids': ['hub_meeting', 'hub_decision']})
    assert response.status_code == 200
    assert client.post('/api/agent-hub/agents/hub_radar/run', json={'request': '확인'}).status_code == 429


def test_long_sources_are_bounded_and_reported_and_memory_is_opt_in(hub):
    client, ctx = hub
    for index in range(20):
        document(ctx.store, f'doc_{index}', 'ERP', '가' * 9000)
    ctx.store.memory('업무 선호', '오전 회의')
    client.patch('/api/agent-hub/agents/hub_meeting', json={'project': 'ERP', 'use_memory': True})
    output = run(client)
    assert sum(len(s['excerpt']) for s in output['sources']) <= 64000
    assert output['scope_warnings'] and output['verification']['status'] == 'needs_review'
    assert any(s.get('truncated') for s in output['sources'])
    # With short documents the explicitly enabled memory enters the scope.
    for doc in ctx.store.all('documents'):
        ctx.store.delete('documents', doc['id'])
    output = run(client)
    assert any(s['type'] == 'memory' for s in output['sources'])
