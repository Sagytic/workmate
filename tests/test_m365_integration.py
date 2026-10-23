from pathlib import Path

from fastapi.testclient import TestClient

from backend import cloud
from backend.llm import demo_plan


def test_new_specialists_route_by_intent_without_broadening_custom_scope():
    agents = [{'id': 'hub_' + kind, 'kind': kind, 'name': name, 'project': ''} for kind, name in (
        ('inbox', '메일 정리·답장 초안'), ('meeting_prep','회의 사전 준비'),
        ('followup','약속·후속 연락'), ('focus','일정 맞춤 집중 계획'))]
    for text, kind in [('메일 정리 부탁해요','inbox'),('회의 준비해 주세요','meeting_prep'),
                       ('회신 추적해 주세요','followup'),('집중 계획 세워줘','focus')]:
        assert demo_plan(text, [], agents)['agent_id'] == 'hub_' + kind


def test_mail_and_calendar_import_and_connection_config_are_visitor_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, 'SESSIONS', tmp_path/'sessions')
    with TestClient(cloud.app) as a, TestClient(cloud.app) as b:
        assert a.get('/').status_code == b.get('/').status_code == 200
        assert not a.get('/api/m365/status').json()['connected']
        response = a.post('/api/m365/config', json={'client_id':'12345678-1234-1234-1234-123456789012','tenant':'organizations'})
        assert response.status_code == 200, response.text
        assert not b.get('/api/m365/status').json()['configured']
        assert a.post('/api/m365/config', json={'client_id':'12345678-1234-1234-1234-123456789012','tenant':'evil.com/../common'}).status_code == 400
        examples = a.get('/api/m365/examples').json()['examples']
        assert len(examples) == 2
        for example in examples:
            payload = a.get(example['download_url']).content
            result = a.post('/api/m365/files', files={'file':(example['filename'],payload)}, data={'project':'ERP 전환'})
            assert result.status_code == 200, result.text
            result = result.json()
            assert result['created'] >= 1
            for doc in result['documents']:
                assert b.get('/api/documents/' + doc['id']).status_code == 404
                assert doc['id'] not in b.get('/api/state').text
            repeat = a.post('/api/m365/files', files={'file':(example['filename'],payload)}, data={'project':'ERP 전환'}).json()
            assert repeat['created'] == 0 and repeat['existing'] == len(result['documents'])
        assert a.post('/api/m365/import', json={'message_ids':['other-visitor-mail']}).status_code == 400
        assert a.post('/api/m365/config', json={'client_id':'12345678-1234-1234-1234-123456789012'}, headers={'Origin':'https://evil.example'}).status_code == 403
        assert not list(tmp_path.rglob('.env'))
