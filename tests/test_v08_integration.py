from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend import cloud
from backend.agent_hub import prepare_run, seed_agent_hub
from backend.m365 import M365Connector, import_items
from backend.personal_desk import WRITING_ID
from backend.store import Store


def test_google_callback_and_desk_records_stay_in_the_starting_visitor(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, 'SESSIONS', tmp_path / 'sessions')
    monkeypatch.setenv('WORKMATE_PUBLIC_HOST', 'workmate.example')
    with TestClient(cloud.app) as a, TestClient(cloud.app) as b:
        assert a.get('/').status_code == b.get('/').status_code == 200
        assert a.post('/api/google/config', json={
            'client_id': '123-workmate.apps.googleusercontent.com',
            'client_secret': 'placeholder_secret_123',
        }).status_code == 200
        authorization = a.post('/api/google/auth/start').json()
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(authorization['authorization_url']).query)['state'][0]
        assert b.get('/api/google/auth/callback', params={'state': state, 'code': 'unused'}, follow_redirects=False).status_code == 400
        assert a.get('/api/google/status').json()['auth']['status'] == 'pending'
        cancelled = a.get('/api/google/auth/callback', params={'state': state, 'error': 'access_denied'}, follow_redirects=False)
        assert cancelled.status_code == 303
        assert cancelled.headers['Referrer-Policy'] == 'no-referrer'
        assert cancelled.headers['Cache-Control'] == 'no-store'
        assert not b.get('/api/google/status').json()['configured']
        writing = {'enabled': True, 'tone': 'private-writing-tone', 'language': '한국어', 'length': '짧게', 'signature': '', 'snippets': []}
        assert a.patch('/api/personal-desk/writing', json=writing).status_code == 200
        assert 'private-writing-tone' not in b.get('/api/personal-desk/state').text
        payload = a.get('/api/m365/examples/project-mail').content
        imported = a.post('/api/m365/files', files={'file': ('mail.eml', payload)}, data={'project': 'ERP'}).json()
        document = imported['documents'][0]
        follow = a.post('/api/personal-desk/followups', json={
            'title': 'private-followup', 'document_id': document['id'], 'direction': 'owed',
        })
        assert follow.status_code == 200, follow.text
        identity = follow.json()['id']
        assert b.patch('/api/personal-desk/followups/' + identity, json={'status': 'resolved'}).status_code == 404
        assert 'private-followup' not in b.get('/api/personal-desk/state').text
        assert b.post('/api/personal-desk/briefings', json={'document_ids': [document['id']]}).status_code == 404
        assert b.post('/api/google/config', json={'client_id': '123-a.apps.googleusercontent.com', 'client_secret': 'placeholder_secret_123'}, headers={'Origin': 'https://other.example'}).status_code == 403
        for path in tmp_path.rglob('*.db'):
            assert b'placeholder_secret_123' not in path.read_bytes()


def test_writing_style_snapshot_is_scoped_and_immutable(tmp_path):
    store = Store(tmp_path / 'writing.db')
    seed_agent_hub(store)
    style = {'id': WRITING_ID, 'enabled': True, 'tone': 'concise-original', 'language': '한국어', 'length': '짧게',
             'signature': '업무 담당자', 'snippets': [{'title': '감사', 'text': '확인해 주셔서 감사합니다.'}]}
    store.put('preferences', style)
    ctx = SimpleNamespace(store=store, planner=SimpleNamespace(configured=True))
    mail = prepare_run(ctx, 'hub_inbox', '답장 초안 준비')
    follow = prepare_run(ctx, 'hub_followup', '후속 연락 준비')
    radar = prepare_run(ctx, 'hub_radar', '현재 업무 확인')
    assert mail['writing_preferences']['tone'] == follow['writing_preferences']['tone'] == 'concise-original'
    assert radar['writing_preferences'] is None
    store.update('preferences', WRITING_ID, tone='new-style')
    assert mail['writing_preferences']['tone'] == 'concise-original'
    store.update('preferences', WRITING_ID, enabled=False)
    assert prepare_run(ctx, 'hub_inbox', '다음 답장')['writing_preferences'] is None


def test_provider_import_counters_do_not_mix_google_with_microsoft(tmp_path):
    store = Store(tmp_path / 'providers.db')
    records = [{'id': 'mail-' + provider, 'kind': 'email', 'provider': provider, 'subject': provider,
                'from': 'person@example.com', 'to': [], 'body': '출처 자료', 'received_at': None}
               for provider in ('google', 'm365', 'eml')]
    import_items(store, records)
    counts = M365Connector(store).status()['imported']
    assert counts['messages'] == counts['total'] == 2
