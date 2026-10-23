"""Real official-SDK client tests over TCP, with isolated stores and workspaces."""
import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from openpyxl import Workbook

from backend.agent_hub import seed_agent_hub, KINDS
from backend.mcp_gateway import Gateway, authenticate, install_mcp, tool_name, PUBLICATIONS
from backend.orchestrator import Engine
from backend.store import Store, KST, now


class NoModel:
    configured = False
    key = 'private-provider-secret'
    mode = 'rules'


def workspace(root):
    ctx = SimpleNamespace(ROOT=root, HOSTED=True, PUBLIC_HOST='', store=Store(root / 'data' / 'workmate.db'), planner=NoModel())
    ctx.engine = Engine(root, ctx.store, ctx.planner)
    seed_agent_hub(ctx.store)
    for identity in ('selected', 'hidden'):
        ctx.store.put('documents', {'id': identity, 'title': identity, 'body': '결정: ' + identity + ' private evidence', 'project': 'P', 'updated_at': now()})
    ctx.store.put('tasks', {'id': 'hidden_task', 'title': 'SECRET_TASK', 'status': 'todo', 'project': 'P', 'owner': 'SECRET_OWNER', 'updated_at': now()})
    ctx.store.memory('SECRET_MEMORY', 'SECRET_VALUE')
    ctx.store.put('apps', {'id': 'selected_app', 'name': 'Selected app', 'schema': {'fields': [{'name': 'note', 'type': 'text'}]}, 'source_path': 'C:/PRIVATE_PATH/secret.html'})
    ctx.store.put('apps', {'id': 'hidden_app', 'name': 'Hidden app', 'schema': {'fields': [{'name': 'note', 'type': 'text'}]}})
    ctx.store.put('records', {'id': 'record1', 'app_id': 'selected_app', 'note': 'VISIBLE_RECORD', 'internal_token': 'NEVER_EXPOSE'})
    ctx.store.put('records', {'id': 'record2', 'app_id': 'hidden_app', 'note': 'HIDDEN_RECORD'})
    folder = root / 'data' / 'fixtures'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('baseline', 'clean', 'changed'):
        book = Workbook()
        book.active.append(['ID', 'Name', 'Amount'])
        book.active.append(['001', name, 10])
        book.save(folder / (name + '.xlsx'))
        book.close()
    return ctx


@pytest.fixture
def live(tmp_path):
    first, second = workspace(tmp_path / 'a'), workspace(tmp_path / 'b')
    def resolve(auth):
        return authenticate(first, auth) or authenticate(second, auth)
    gateway = Gateway(resolve)
    @asynccontextmanager
    async def lifespan(app):
        async with gateway.server.session_manager.run():
            yield
    app = FastAPI(lifespan=lifespan)
    # Two owner management interfaces; transport shares no cookie authority.
    a, b = FastAPI(), FastAPI()
    install_mcp(a, first)
    install_mcp(b, second)
    app.mount('/owner-a', a)
    app.mount('/owner-b', b)
    app.mount('/mcp', gateway)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    base = f'http://127.0.0.1:{port}'
    yield SimpleNamespace(base=base, first=first, second=second, gateway=gateway)
    server.should_exit = True
    thread.join(timeout=10)
    first.engine.stop()
    second.engine.stop()
    sock.close()


def publish(live, selections, owner='a', **extra):
    response = httpx.post(live.base + '/owner-' + owner + '/api/mcp/publications', json={
        'name': 'Test connection', 'selections': [{'kind': kind, 'id': identity} for kind, identity in selections], **extra})
    assert response.status_code == 200, response.text
    return response.json()


@asynccontextmanager
async def client(live, key, mode='legacy'):
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + key}, timeout=20) as http:
        async with Client(streamable_http_client(live.base + '/mcp/', http_client=http), mode=mode) as mcp:
            yield mcp


async def completed(mcp, run_id):
    for _ in range(100):
        result = await mcp.call_tool('get_run', {'run_id': run_id})
        assert not result.is_error, result
        if result.structured_content['status'] not in ('running', 'queued'):
            return result.structured_content
        await asyncio.sleep(.02)
    raise AssertionError('Run did not finish')


@pytest.mark.parametrize('mode', ['legacy', 'auto'])
def test_official_client_tools_resources_and_no_context(live, mode):
    chosen = [('agent', 'hub_radar'), ('agent', 'hub_meeting'), ('document', 'selected'), ('app', 'selected_app')]
    pub = publish(live, chosen)
    live.first.store.update('hub_agents', 'hub_radar', project='P', document_ids=['hidden'], use_memory=True)
    async def check():
        async with client(live, pub['access_key'], mode) as mcp:
            tools = await mcp.list_tools()
            assert {tool.name for tool in tools.tools} == {tool_name('hub_radar'), tool_name('hub_meeting'), 'get_run', 'app_records'}
            resources = await mcp.list_resources()
            assert {str(resource.uri) for resource in resources.resources} == {'workmate://document/selected', 'workmate://app/selected_app'}
            data = await mcp.read_resource('workmate://document/selected')
            assert 'selected private evidence' in data.contents[0].text
            schema = await mcp.read_resource('workmate://app/selected_app')
            assert 'source_path' not in schema.contents[0].text and 'PRIVATE_PATH' not in schema.contents[0].text
            denied = await mcp.call_tool('app_records', {'app_id': 'hidden_app'})
            assert denied.is_error
            rows = await mcp.call_tool('app_records', {'app_id': 'selected_app', 'limit': 1})
            assert rows.structured_content['total'] == 1
            assert 'VISIBLE_RECORD' in str(rows) and 'NEVER_EXPOSE' not in str(rows)
            run = await mcp.call_tool(tool_name('hub_radar'), {'request': '이번 요청만 분석'})
            assert not run.is_error, run
            done = await completed(mcp, run.structured_content['id'])
            assert done['status'] == 'completed'
            assert {source['type'] for source in done['sources']} == {'request'}
            assert 'SECRET' not in json.dumps(done) and 'hidden' not in json.dumps(done)
            denied = await mcp.call_tool(tool_name('hub_decision'), {'request': 'forbidden'})
            assert denied.is_error
            try:
                await mcp.read_resource('workmate://document/hidden')
                raise AssertionError('Unselected resource returned')
            except Exception as exc:
                assert not isinstance(exc, AssertionError)
    asyncio.run(check())
    saved = live.first.store.get(PUBLICATIONS, pub['publication']['id'])
    assert pub['access_key'] not in json.dumps(saved)
    assert saved['request_count'] > 0 and saved['recent_activity']


def test_scoped_run_ids_rotation_revocation_expiry_and_tenants(live):
    a = publish(live, [('agent', 'hub_meeting'), ('document', 'selected')])
    b = publish(live, [('agent', 'hub_meeting'), ('document', 'hidden')], owner='b')
    live.second.store.update('documents', 'hidden', body='SECOND_TENANT_ONLY')
    async def check():
        async with client(live, a['access_key']) as first, client(live, b['access_key']) as second:
            ar, br = await asyncio.gather(first.list_resources(), second.list_resources())
            assert [str(r.uri) for r in ar.resources] == ['workmate://document/selected']
            assert [str(r.uri) for r in br.resources] == ['workmate://document/hidden']
            run = await first.call_tool(tool_name('hub_meeting'), {'request': '결정: FIRST_TENANT_ONLY'})
            denied = await second.call_tool('get_run', {'run_id': run.structured_content['id']})
            assert denied.is_error
            denied = await first.call_tool('get_run', {'run_id': 'hidden_task'})
            assert denied.is_error
            rotated = httpx.post(live.base + '/owner-a/api/mcp/publications/' + a['publication']['id'] + '/rotate').json()
            async with client(live, rotated['access_key']) as newer:
                denied = await newer.call_tool('get_run', {'run_id': run.structured_content['id']})
                assert denied.is_error
            old = httpx.post(live.base + '/mcp/', headers={'Authorization': 'Bearer ' + a['access_key']}, json={})
            assert old.status_code == 401
            assert httpx.post(live.base + '/owner-a/api/mcp/publications/' + a['publication']['id'] + '/revoke').status_code == 200
            assert httpx.get(live.base + '/mcp/', headers={'Authorization': 'Bearer ' + rotated['access_key']}).status_code == 401
    asyncio.run(check())
    live.second.store.update(PUBLICATIONS, b['publication']['id'], expires_at=(datetime.now(KST) - timedelta(seconds=1)).isoformat())
    assert httpx.post(live.base + '/mcp/', headers={'Authorization': 'Bearer ' + b['access_key']}, json={}).status_code == 401


def test_context_optin_is_frozen_and_excel_document_tools(live):
    live.first.store.update('hub_agents', 'hub_meeting', project='P', document_ids=['selected'], use_memory=True)
    pub = publish(live, [('agent', 'hub_meeting'), ('tool', 'excel'), ('file', 'baseline'), ('file', 'clean'),
                        ('tool', 'knowledge'), ('tool', 'analyst'), ('document', 'selected'), ('document', 'hidden')], include_context=True)
    live.first.store.update('hub_agents', 'hub_meeting', document_ids=['hidden'])
    live.first.store.update('documents', 'selected', body='NEW_BODY_MUST_NOT_ENTER_FROZEN_CONTEXT')
    async def check():
        async with client(live, pub['access_key']) as mcp:
            response = await mcp.call_tool(tool_name('hub_meeting'), {'request': '결정: 요청'})
            done = await completed(mcp, response.structured_content['id'])
            text = json.dumps(done, ensure_ascii=False)
            assert 'selected private evidence' in text and 'NEW_BODY_MUST_NOT' not in text
            assert 'SECRET_TASK' in text and 'SECRET_VALUE' in text
            denied = await mcp.call_tool('excel_validate', {'baseline_id': 'baseline', 'current_id': 'changed'})
            assert denied.is_error
            denied = await mcp.call_tool('excel_validate', {'baseline_id': '../data', 'current_id': 'clean'})
            assert denied.is_error
            response = await mcp.call_tool('excel_validate', {'baseline_id': 'baseline', 'current_id': 'clean'})
            done = await completed(mcp, response.structured_content['id'])
            assert done['status'] == 'completed', done
            assert done['result']['recalculated'] is False
            assert 'artifacts' not in done['result']
            denied = await mcp.call_tool('knowledge_search', {'question': 'anything', 'document_ids': []})
            assert denied.is_error
            response = await mcp.call_tool('knowledge_search', {'question': 'evidence', 'document_ids': ['hidden']})
            done = await completed(mcp, response.structured_content['id'])
            assert done['status'] == 'completed', done
            response = await mcp.call_tool('document_compare', {'before_id': 'selected', 'after_id': 'hidden'})
            done = await completed(mcp, response.structured_content['id'])
            assert done['status'] == 'completed', done
    asyncio.run(check())


def test_http_boundary_and_no_secret_in_management(live):
    pub = publish(live, [('document', 'selected')])
    headers = {'Authorization': 'Bearer ' + pub['access_key']}
    for path in ('/mcp/', '/mcp/bad'):
        assert httpx.get(live.base + path, headers={'Cookie': 'workmate_session=anything', 'Mcp-Session-Id': 'anything'}).status_code == 401
    assert httpx.post(live.base + '/mcp/', headers={**headers, 'Origin': 'https://evil.test'}, json={}).status_code == 403
    assert httpx.post(live.base + '/mcp/?access_token=never', headers=headers, json={}).status_code == 400
    assert httpx.post(live.base + '/mcp/', headers=headers, content=b'x' * (256 * 1024 + 1)).status_code == 413
    listing = httpx.get(live.base + '/owner-a/api/mcp/publications').text
    config = httpx.get(live.base + '/owner-a/api/mcp/publications/' + pub['publication']['id'] + '/config').text
    assert pub['access_key'] not in listing + config
    assert 'key_hash' not in listing + config and 'snapshots' not in listing
    assert '<WORKMATE_MCP_KEY>' in config


def test_cloud_unauthenticated_mcp_never_bootstraps(tmp_path, monkeypatch):
    from backend import cloud
    monkeypatch.setattr(cloud, 'SESSIONS', tmp_path / 'sessions')
    before = len(cloud.VISITORS)
    # No lifespan needed for a request rejected before reaching the SDK manager.
    client = TestClient(cloud.app)
    assert client.get('/mcp/').status_code == 401
    assert client.get('/mcp').status_code == 401
    assert client.post('/mcp', json={}).status_code == 401
    assert client.get('/mcp/bad').status_code == 401
    assert client.post('/mcp/', json={}).status_code == 401
    assert len(cloud.VISITORS) == before


def test_every_builtin_and_custom_agent_defaults_to_request_only(live):
    live.first.store.put('preferences', {'id': 'desk_writing', 'enabled': True, 'signature': 'SECRET_SIGNATURE'})
    for profile in live.first.store.all('hub_agents'):
        live.first.store.update('hub_agents', profile['id'], project='P', document_ids=['hidden'], use_memory=True)
    custom = {**live.first.store.get('hub_agents', 'hub_meeting'), 'id': 'custom_meeting', 'name': 'Custom meeting', 'builtin': False}
    live.first.store.put('hub_agents', custom)
    identities = ['hub_' + kind['id'] for kind in KINDS] + ['custom_meeting']
    pub = publish(live, [('agent', identity) for identity in identities])
    assert pub['publication']['context_summary'] == {'documents': 0, 'tasks': 0, 'memories': 0, 'writing_preferences': False}
    async def check():
        async with client(live, pub['access_key']) as mcp:
            for identity in identities:
                response = await mcp.call_tool(tool_name(identity), {'request': '결정: 이번 요청만 분석'})
                assert not response.is_error, (identity, response)
                result = await completed(mcp, response.structured_content['id'])
                assert result['status'] == 'completed', result
                stored = live.first.store.get('hub_runs', result['id'])
                assert stored['writing_preferences'] is None
                assert {source['type'] for source in result['sources']} == {'request'}
                assert 'SECRET' not in json.dumps(result)
    asyncio.run(check())


def test_real_cloud_gateway_uses_bearer_workspace_and_destroy_expires_key(tmp_path, monkeypatch):
    from backend import cloud
    monkeypatch.setattr(cloud, 'SESSIONS', tmp_path / 'cloud')
    monkeypatch.setattr(cloud, 'VISITORS', {})
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(cloud.app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    try:
        with httpx.Client(base_url=base, timeout=30) as a, httpx.Client(base_url=base, timeout=30) as b:
            assert a.get('/').status_code == 200 and b.get('/').status_code == 200
            assert len(cloud.VISITORS) == 2
            first = a.post('/api/mcp/publications', json={'name': 'A', 'selections': [{'kind': 'agent', 'id': 'hub_meeting'}]}).json()
            second = b.post('/api/mcp/publications', json={'name': 'B', 'selections': [{'kind': 'agent', 'id': 'hub_radar'}]}).json()
            assert a.get('/mcp/').status_code == 401  # Browser session alone is not MCP authorization.
            async def check():
                # A conflicting B browser cookie must not substitute for bearer A.
                async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + first['access_key'], 'Cookie': 'workmate_session=' + b.cookies['workmate_session']}) as http:
                    async with Client(streamable_http_client(base + '/mcp/', http_client=http), mode='legacy') as mcp:
                        names = {tool.name for tool in (await mcp.list_tools()).tools}
                        assert tool_name('hub_meeting') in names and tool_name('hub_radar') not in names
                        result = await mcp.call_tool(tool_name('hub_meeting'), {'request': '결정: hosted MCP works'})
                        assert not result.is_error, result
                        done = await completed(mcp, result.structured_content['id'])
                        assert done['status'] == 'completed'
            asyncio.run(check())
            sid = a.cookies['workmate_session']
            assert cloud.VISITORS[sid]['module'].store.all('mcp_run_grants')
            assert not cloud.VISITORS[b.cookies['workmate_session']]['module'].store.all('mcp_run_grants')
            cloud.destroy(sid, cloud.VISITORS.pop(sid))
            assert httpx.get(base + '/mcp/', headers={'Authorization': 'Bearer ' + first['access_key']}).status_code == 401
            assert b.get('/api/mcp/publications').json()['publications'][0]['id'] == second['publication']['id']
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
