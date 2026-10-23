import time

from fastapi.testclient import TestClient

from backend import cloud
from backend.llm import demo_plan


def test_generic_and_explicit_requests_route_to_correct_scope():
    agents = [
        {'id': 'custom_b', 'name': '고객 B 회의 담당', 'kind': 'meeting', 'project': 'B'},
        {'id': 'custom_a', 'name': '고객 A 회의 담당', 'kind': 'meeting', 'project': 'A'},
        {'id': 'hub_meeting', 'name': '회의 후속 정리', 'kind': 'meeting', 'project': ''},
    ]
    assert demo_plan('회의록 정리해줘', [], agents)['agent_id'] == 'hub_meeting'
    assert demo_plan('고객 A 회의 담당에게 회의록 정리를 맡겨줘', [], agents)['agent_id'] == 'custom_a'
    agents[-1]['archived'] = True
    assert demo_plan('회의록 정리해줘', [], agents)['action'] != 'agent'


def test_hosted_hub_chat_mission_and_artifacts_are_visitor_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, 'SESSIONS', tmp_path / 'sessions')
    with TestClient(cloud.app) as a, TestClient(cloud.app) as b:
        assert a.get('/').status_code == b.get('/').status_code == 200
        assert a.get('/api/state').json()['hub_summary']['agents'] == 8
        reply = a.post('/api/chat', json={'message': '회의록 정리\n결정: A 전용 승인 기준\n액션: 승인 기준 작성 | 담당: 서연 | 기한: 2026-10-28'}).json()
        rid = reply['hub_run_id']
        for _ in range(100):
            run = a.get('/api/agent-hub/runs/' + rid).json()
            if run['status'] not in ('queued', 'running'):
                break
            time.sleep(.02)
        assert run['status'] == 'completed'
        assert b.get('/api/agent-hub/runs/' + rid).status_code == 404
        assert b.get('/api/agent-hub/runs/' + rid + '/report.html').status_code == 404
        assert 'A 전용 승인 기준' in a.get('/api/agent-hub/runs/' + rid + '/report.html').text
        assert any(n.get('hub_run_id') == rid for n in a.get('/api/companion').json()['nudges'])
        assert 'A 전용 승인 기준' not in b.get('/api/agent-hub').text
        added = a.post('/api/agent-hub/runs/' + rid + '/actions', json={'indices': [0]}).json()
        tid = added['tasks'][0]['id']
        edited = a.patch('/api/tasks/' + tid, json={'due_at': '2026-10-29'})
        assert edited.status_code == 200, edited.text
        assert edited.json()['due_at'] == '2026-10-29'
        m = a.post('/api/agent-hub/missions', json={'request': '결정: 공동 검토', 'agent_ids': ['hub_decision', 'hub_reflection']})
        assert m.status_code == 200, m.text
        mid = m.json()['id']
        assert b.get('/api/agent-hub/missions/' + mid).status_code == 404
