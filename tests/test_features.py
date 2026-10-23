import csv
import io
import json
from datetime import datetime, timedelta
from unittest.mock import Mock
from zipfile import ZipFile
import pytest
from backend import app as api
from backend.store import KST
from backend.llm import Planner
from test_platform import client, wait, make_app


def test_history_survives_new_store_and_clear_does_not_erase_memory(client):
    client.post('/api/chat', json={'message': '기억해줘 출력 양식: 한 문장'})
    history = client.get('/api/chat/history').json()
    assert [m['role'] for m in history] == ['user', 'assistant']
    assert api.Store(api.store.path).all('messages')[0]['id'] == history[-1]['id']
    assert client.delete('/api/chat/history').status_code == 200
    assert client.get('/api/chat/history').json() == []
    assert any(m['key'] == '출력 양식' for m in api.store.all('memories'))


def test_task_edit_archive_retry_and_bundle(client):
    task = wait(client, client.post('/api/tasks', json={'agent': 'excel', 'input': {'current': 'clean'}}).json()['id'])
    tid = task['id']
    response = client.patch('/api/tasks/' + tid, json={'title': '검수 완료', 'priority': 'high', 'due_at': None, 'archived': True})
    assert response.status_code == 200, response.text
    state = client.get('/api/state').json()
    assert any(t['id'] == tid for t in state['archived_tasks'])
    assert all(t['id'] != tid for t in state['tasks'])
    bundle = client.get(f'/api/tasks/{tid}/bundle')
    with ZipFile(io.BytesIO(bundle.content)) as archive:
        assert 'task.json' in archive.namelist()
        assert any(n.endswith('.csv') for n in archive.namelist())
        assert not any('.env' in n for n in archive.namelist())
    retry = client.post(f'/api/tasks/{tid}/retry').json()
    assert retry['parent_id'] == tid and retry['priority'] == 'high'
    assert wait(client, retry['id'])['status'] == 'completed'
    assert client.patch('/api/tasks/' + tid, json={'priority': 'invalid'}).status_code == 400


def test_batch_artifacts_and_no_bulk_repair(client):
    task = wait(client, client.post('/api/tasks', json={'agent': 'excel', 'input': {'currents': ['changed', 'clean', 'reordered']}}).json()['id'])
    assert task['status'] == 'completed'
    assert len(task['result']['batches']) == 3
    assert task['result']['repairable_count'] == 0
    for artifact in task['artifacts']:
        assert client.get(artifact['url']).status_code == 200
    assert client.post(f"/api/tasks/{task['id']}/resume", json={'action': 'repair'}).status_code == 400


def test_batch_all_files_failed_marks_task_failed_and_keeps_report(client, monkeypatch):
    from backend import excel_agent

    def all_failed(baseline, currents, output_dir):
        result = {
            'status': 'failed', 'verification_status': 'unverified',
            'summary': '모든 파일 검사 실패 · 미검증',
            'batch_counts': {'total_files': 2, 'completed_files': 0, 'failed_files': 2},
            'counts': {'errors': 2, 'warnings': 0, 'changes': 0},
            'findings': [], 'batches': [{'filename': path.name, 'status': 'failed'} for path in currents],
            'artifacts': [{'name': 'inspection.json', 'kind': 'json'}],
        }
        (output_dir / 'inspection.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result

    inspect = Mock(side_effect=all_failed)
    monkeypatch.setattr(excel_agent, 'inspect_workbooks', inspect)
    response = client.post('/api/tasks', json={'agent': 'excel', 'input': {'currents': ['clean', 'changed']}})
    assert response.status_code == 200, response.text
    task = wait(client, response.json()['id'])
    assert task['status'] == 'failed', task
    assert task['result']['status'] == 'failed'
    assert task['result']['verification_status'] == 'unverified'
    assert task['result']['batch_counts']['failed_files'] == 2
    inspect.assert_called_once()
    assert len(task['artifacts']) == 1
    saved = client.get(task['artifacts'][0]['url'])
    assert saved.status_code == 200
    assert saved.json()['status'] == 'failed'


def test_interval_pause_reschedule_and_saved_inputs(client):
    workflow = client.post('/api/workflows', json={'name': '주기 검사', 'trigger': 'interval', 'interval_minutes': 1, 'current': 'clean'}).json()
    wid = workflow['id']
    api.store.update('workflows', wid, next_run_at=(datetime.now(KST) - timedelta(minutes=1)).isoformat())
    assert api.engine.check_due_workflows() == 1
    assert api.engine.check_due_workflows() == 0
    task = next(t for t in api.store.all('tasks') if t['input'].get('workflow_id') == wid)
    assert wait(client, task['id'])['status'] == 'completed'
    assert task['input']['workflow_snapshot']['current'] == 'clean'
    manual = client.post(f'/api/workflows/{wid}/run', json={}).json()
    assert manual['input']['current'] == 'clean'
    assert wait(client, manual['id'])['status'] == 'completed'
    assert client.patch(f'/api/workflows/{wid}', json={'enabled': False}).status_code == 200
    api.store.update('workflows', wid, next_run_at=(datetime.now(KST) - timedelta(minutes=1)).isoformat())
    assert api.engine.check_due_workflows() == 0
    assert client.post(f'/api/workflows/{wid}/run', json={}).status_code == 400
    assert client.patch(f'/api/workflows/{wid}', json={'enabled': None}).status_code == 400
    assert client.delete(f'/api/workflows/{wid}').status_code == 200


def test_workflow_repeat_deduplicates_per_target_app(client):
    first, second = make_app(client), make_app(client)
    workflow = client.post('/api/workflows', json={'name': '중복 방지', 'app_id': first, 'current': 'clean'}).json()
    wid = workflow['id']
    for _ in range(2):
        task = client.post(f'/api/workflows/{wid}/run', json={}).json()
        assert wait(client, task['id'])['status'] == 'completed'
    assert len(client.get(f'/api/apps/{first}/records').json()) == 1
    client.patch(f'/api/workflows/{wid}', json={'app_id': second})
    task = client.post(f'/api/workflows/{wid}/run', json={}).json()
    assert wait(client, task['id'])['status'] == 'completed'
    assert len(client.get(f'/api/apps/{second}/records').json()) == 1


def test_workflow_custom_required_field_waits_then_resumes_registration(client):
    app_id = make_app(client)
    business_app = api.store.get('apps', app_id)
    business_app['schema']['fields'].append(
        {'name': 'customer', 'label': '고객명', 'type': 'text', 'required': True})
    api.store.put('apps', business_app)
    workflow = client.post('/api/workflows', json={
        'name': '고객별 검수 등록', 'app_id': app_id, 'current': 'clean'}).json()
    response = client.post(f"/api/workflows/{workflow['id']}/run", json={})
    assert response.status_code == 200, response.text
    task = wait(client, response.json()['id'])
    assert task['status'] == 'waiting_input', task
    assert task['result']['checkpoint'] == 'registration'
    assert task['result']['findings'] == []
    registration = task['result']['registration']
    assert registration['app_id'] == app_id
    assert '고객명' in registration['error']
    assert any(field['name'] == 'customer' and field['required'] for field in registration['fields'])
    assert client.get(f'/api/apps/{app_id}/records').json() == []
    initial_artifacts = {artifact['name']: artifact['url'] for artifact in task['artifacts']}
    assert any(name.endswith('inspection.html') for name in initial_artifacts)
    assert any(name.endswith('findings.csv') for name in initial_artifacts)
    for url in initial_artifacts.values():
        assert client.get(url).status_code == 200

    resumed = client.post(f"/api/tasks/{task['id']}/resume", json={
        'action': 'continue', 'input': {'record_values': {'customer': '합성 고객 A'}}})
    assert resumed.status_code == 200, resumed.text
    finished = wait(client, task['id'])
    assert finished['status'] == 'completed', finished
    assert finished['result']['checkpoint'] == 'completed'
    records = client.get(f'/api/apps/{app_id}/records').json()
    assert len(records) == 1
    assert records[0]['customer'] == '합성 고객 A'
    assert records[0]['id'] == finished['result']['record_id']
    assert set(initial_artifacts).issubset({artifact['name'] for artifact in finished['artifacts']})
    for url in initial_artifacts.values():
        assert client.get(url).status_code == 200


def test_workflow_prompt_edit_persists_as_description(client):
    workflow = client.post('/api/workflows', json={
        'name': '설명 편집', 'prompt': '변경 전 설명'}).json()
    edited = client.patch(f"/api/workflows/{workflow['id']}", json={'prompt': '검수 후 보완을 기다리는 새 설명'})
    assert edited.status_code == 200, edited.text
    assert edited.json()['description'] == '검수 후 보완을 기다리는 새 설명'
    persisted = next(flow for flow in client.get('/api/state').json()['workflows'] if flow['id'] == workflow['id'])
    assert persisted['description'] == edited.json()['description']
    assert persisted['name'] == '설명 편집'


@pytest.mark.parametrize('legacy_without_snapshot', [False, True])
def test_waiting_workflow_can_be_deleted_then_resumed_from_snapshot(client, monkeypatch, legacy_without_snapshot):
    app_id = make_app(client)
    workflow = client.post('/api/workflows', json={
        'name': '삭제 후 이어서 검수', 'app_id': app_id, 'current': 'changed'}).json()
    wid = workflow['id']
    task = wait(client, client.post(f'/api/workflows/{wid}/run', json={}).json()['id'])
    assert task['status'] == 'waiting_input'
    assert task['result']['checkpoint'] == 'validation'
    if legacy_without_snapshot:
        inputs = {key: value for key, value in task['input'].items() if key != 'workflow_snapshot'}
        api.store.update('tasks', task['id'], input=inputs)

    removed = client.delete(f'/api/workflows/{wid}')
    assert removed.status_code == 200, removed.text
    assert api.store.get('workflows', wid) is None
    saved_task = client.get(f"/api/tasks/{task['id']}").json()
    assert saved_task['input']['workflow_snapshot']['id'] == wid
    assert saved_task['input']['workflow_snapshot']['app_id'] == app_id
    update = Mock(wraps=api.store.update)
    monkeypatch.setattr(api.store, 'update', update)
    resumed = client.post(f"/api/tasks/{task['id']}/resume", json={
        'action': 'continue', 'input': {'current': 'clean'}})
    assert resumed.status_code == 200, resumed.text
    finished = wait(client, task['id'])
    assert finished['status'] == 'completed', finished
    assert finished['result']['checkpoint'] == 'completed'
    assert api.store.get('workflows', wid) is None
    assert not any(call.args[:2] == ('workflows', wid) for call in update.call_args_list)
    records = client.get(f'/api/apps/{app_id}/records').json()
    assert len(records) == 1
    assert records[0]['id'] == finished['result']['record_id']


def test_dynamic_validation_delete_and_csv_injection(client):
    app_id = make_app(client)
    app = api.store.get('apps', app_id)
    app['schema']['fields'] += [
        {'name': 'amount', 'label': '금액', 'type': 'number', 'required': True, 'min': -10, 'max': 100},
        {'name': 'contact', 'label': '이메일', 'type': 'email', 'required': True},
        {'name': 'consent', 'label': '확인', 'type': 'checkbox', 'required': True}]
    api.store.put('apps', app)
    url = f'/api/apps/{app_id}/records'
    payload = {'title': '=HYPERLINK("bad")', 'status': '대기', 'amount': 0, 'contact': 'person@example.test', 'consent': True}
    for key, value in [('amount', 101), ('amount', 'NaN'), ('contact', 'broken'), ('consent', 'true'), ('consent', False)]:
        assert client.post(url, json={**payload, key: value}).status_code == 400
    record = client.post(url, json={**payload, 'amount': -5}).json()
    exported = client.get(f'/api/apps/{app_id}/export.csv')
    rows = list(csv.reader(io.StringIO(exported.content.decode('utf-8-sig'))))
    assert rows[1][0].startswith("'=")
    assert '-5.0' in rows[1]
    assert client.delete(url + '/' + record['id']).status_code == 200
    assert client.get(url).json() == []


def test_upcoming_and_overdue_notifications_are_distinct(client):
    task = client.post('/api/tasks', json={'agent': 'assistant', 'input': {'manual': True}, 'due_at': (datetime.now(KST) + timedelta(minutes=30)).isoformat()}).json()
    client.patch('/api/tasks/' + task['id'], json={'due_at': (datetime.now(KST) - timedelta(minutes=1)).isoformat()})
    api.engine.check_notifications()
    notes = [n for n in api.store.all('notifications') if n.get('task_id') == task['id']]
    assert {n['title'] for n in notes} == {'마감 예정', '기한이 되었습니다'}
    assert len(notes) == 2


@pytest.mark.parametrize('status', [401, 402, 429, 500])
def test_llm_errors_do_not_expose_provider_body_or_key(monkeypatch, status):
    import backend.llm as llm
    class Response:
        status_code = status
        text = 'unit-secret should never surface'
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(llm.httpx, 'Client', Client)
    p = Planner()
    p.key, p.base, p.model = 'unit-secret', 'https://mock.invalid/v1', 'mock'
    with pytest.raises(ValueError) as exc:
        p.test_connection()
    assert 'unit-secret' not in str(exc.value) + str(p.public_status())
    assert p.public_status()['status'] == 'error'
