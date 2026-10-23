import io
from datetime import datetime, timedelta
from backend import app as api
from backend.store import KST, now
from test_platform import client, wait


def document(client, title='검수 기준', body='배포 전 사용자 승인과 검수 증빙이 필요합니다.', project='ERP 전환'):
    response = client.post('/api/documents', json={'title': title, 'body': body, 'project': project})
    assert response.status_code == 200, response.text
    return response.json()


def test_document_crud_unicode_upload_and_download(client):
    doc = document(client)
    did = doc['id']
    assert client.get('/api/documents/' + did).json()['body'] == doc['body']
    assert client.patch('/api/documents/' + did, json={'body': '변경한 검수 기준'}).status_code == 200
    text = client.get(f'/api/documents/{did}/download').content.decode('utf-8-sig')
    assert '변경한 검수 기준' in text
    uploaded = client.post('/api/documents/upload', files={'file': ('기준.txt', io.BytesIO('한국어 자료'.encode('cp949')))}, data={'project': 'ERP 전환'})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()['body'] == '한국어 자료'
    assert client.post('/api/documents/upload', files={'file': ('bad.exe', b'x')}).status_code == 400
    assert client.patch('/api/documents/' + did, json={'title': '  '}).status_code == 400
    assert client.delete('/api/documents/' + did).status_code == 200
    assert client.get('/api/documents/' + did).status_code == 404


def test_real_search_project_filter_and_document_deleted_after_run(client):
    first = document(client, body='승인 검토 기준과 배포 절차')
    second = document(client, title='금융 검토', body='승인 검토 기준', project='금융 정산')
    found = client.get('/api/search', params={'q': '승인 검토', 'project': 'ERP 전환'}).json()
    assert found['total'] == 1 and found['results'][0]['document_id'] == first['id']
    task = wait(client, client.post('/api/tasks', json={'agent': 'knowledge', 'input': {'question': '배포 승인 기준은?', 'document_ids': [first['id']]}}).json()['id'])
    assert task['status'] == 'completed'
    assert task['result']['generation'] == 'rules'
    client.delete('/api/documents/' + first['id'])
    # Run artifacts remain immutable evidence after source editing/deletion.
    assert any('승인 검토 기준' in s['quote'] for s in task['result']['sources'])
    for artifact in task['artifacts']:
        assert client.get(artifact['url']).status_code == 200
    assert client.get('/api/search', params={'q': '승인', 'project': '금융 정산'}).json()['results'][0]['id'] == second['id']


def test_manual_board_state_and_project_counts(client):
    response = client.post('/api/tasks', json={'agent': 'assistant', 'title': '검수 승인 요청', 'project': 'ERP 전환', 'owner': '김검수', 'tags': ['인수'], 'priority': 'high', 'input': {'manual': True}, 'due_at': (datetime.now(KST) - timedelta(days=1)).isoformat()})
    task = response.json()
    assert task['status'] == 'todo'
    assert client.patch('/api/tasks/' + task['id'], json={'status': 'in_progress'}).json()['status'] == 'in_progress'
    project = client.get('/api/projects').json()['projects'][0]
    assert project['open_count'] == 1 and project['overdue_count'] == 1 and project['owners'] == ['김검수']
    complete = client.patch('/api/tasks/' + task['id'], json={'status': 'completed'}).json()
    assert complete['completed_at']
    project = client.get('/api/projects').json()['projects'][0]
    assert project['completed_count'] == 1 and project['overdue_count'] == 0
    api.store.update('tasks', task['id'], completed_at='2026-10-01T10:00:00+09:00')
    edited = client.patch('/api/tasks/' + task['id'], json={'title': '이름만 변경', 'status': 'completed'}).json()
    assert edited['completed_at'] == '2026-10-01T10:00:00+09:00'
    assert client.patch('/api/tasks/' + task['id'], json={'status': 'running'}).status_code == 400
    assert client.post('/api/tasks', json={'agent': 'excel', 'status': 'completed'}).status_code == 400


def test_notes_persist_and_do_not_become_workflow_instructions(client):
    task = client.post('/api/tasks', json={'agent': 'assistant', 'input': {'manual': True}}).json()
    note = client.post(f"/api/tasks/{task['id']}/notes", json={'content': '<script>example</script> 확인 메모'}).json()
    saved = client.get('/api/tasks/' + task['id']).json()
    assert saved['notes'][0]['content'].startswith('<script>')
    assert saved['status'] == 'todo'
    assert client.delete(f"/api/tasks/{task['id']}/notes/{note['id']}").status_code == 200
    assert client.get('/api/tasks/' + task['id']).json()['notes'] == []


def test_change_analysis_followups_import_once_and_preserve_evidence(client):
    before = document(client, '요구사항 v1', '승인자는 한 명입니다.\n\n로그 보관은 30일입니다.')
    after = document(client, '요구사항 v2', '승인자는 두 명입니다.\n\n로그 보관은 90일입니다.')
    task = wait(client, client.post('/api/tasks', json={'agent': 'analyst', 'project': 'ERP 전환', 'input': {'before_id': before['id'], 'after_id': after['id']}}).json()['id'])
    assert task['status'] == 'completed', task
    action_ids = [a['id'] for a in task['result']['actions']]
    assert action_ids
    before_count = len(api.store.all('tasks'))
    first = client.post(f"/api/tasks/{task['id']}/actions/import", json={'action_ids': action_ids}).json()
    second = client.post(f"/api/tasks/{task['id']}/actions/import", json={'action_ids': action_ids}).json()
    assert first['created'] == len(action_ids) and second['created'] == 0
    assert len(api.store.all('tasks')) == before_count + len(action_ids)
    assert all(t['project'] == 'ERP 전환' and t['source_task_id'] == task['id'] and t['status'] == 'todo' for t in first['tasks'])
    assert client.post(f"/api/tasks/{task['id']}/actions/import", json={'action_ids': ['invented']}).status_code == 400


def test_reporter_uses_actual_completion_and_project_scope(client):
    task = client.post('/api/tasks', json={'agent': 'assistant', 'title': 'ERP 검수', 'project': 'ERP 전환', 'input': {'manual': True}}).json()
    client.patch('/api/tasks/' + task['id'], json={'status': 'completed'})
    client.post('/api/tasks', json={'agent': 'assistant', 'title': '다른 금융 업무', 'project': '금융 정산', 'input': {'manual': True}})
    report = wait(client, client.post('/api/tasks', json={'agent': 'reporter', 'project': 'ERP 전환', 'input': {'period_days': 7, 'report_type': 'handover'}}).json()['id'])
    assert report['status'] == 'completed', report
    assert any(s.get('task_id') == task['id'] for s in report['result']['sources'])
    assert all('다른 금융 업무' not in s['quote'] for s in report['result']['sources'])
    assert report['completed_at']


def test_top_level_project_scopes_knowledge_and_conflicts_are_rejected(client):
    chosen = document(client, body='ERP 담당 승인 기준입니다.', project='ERP 전환')
    document(client, body='금융 승인 기준입니다.', project='금융 정산')
    task = wait(client, client.post('/api/tasks', json={'agent': 'knowledge', 'project': 'ERP 전환', 'input': {'question': '승인 기준은?'}}).json()['id'])
    assert {s['document_id'] for s in task['result']['sources']} == {chosen['id']}
    response = client.post('/api/tasks', json={'agent': 'reporter', 'project': 'ERP 전환', 'input': {'project': '금융 정산'}})
    assert response.status_code == 400


def test_new_app_records_are_searchable_in_parent_project(client):
    task = wait(client, client.post('/api/tasks', json={'agent': 'builder', 'project': 'ERP 전환', 'input': {'prompt': '요청 관리'}}).json()['id'])
    app_id = task['result']['app_id']
    record = client.post(f'/api/apps/{app_id}/records', json={'title': '새로운 검색 대상', 'status': '대기'}).json()
    result = client.get('/api/search', params={'q': '새로운 검색 대상', 'project': 'ERP 전환'}).json()['results']
    assert len(result) == 1 and result[0]['id'] == record['id'] and result[0]['project'] == 'ERP 전환'
    assert not client.get('/api/search', params={'q': '새로운 검색 대상', 'project': '금융 정산'}).json()['results']
