import base64
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.google_workspace import (AUTH_URL, CALLBACK_PATH, EVENTS_URL, MAIL_URL,
    MAX_RESPONSE, SCOPES, TOKEN_URL, USER_URL, GoogleWorkspaceConnector, callback_uri,
    install_google_workspace, normalize_event, normalize_message)
from backend.store import Store


CLIENT_ID = '1234567890-ownregisteredclient.apps.googleusercontent.com'
SECRET = 'GOCSPX-private-client-secret'
TOKENS = {'access_token': 'private-access-token', 'refresh_token': 'private-refresh-token',
          'token_type': 'Bearer', 'expires_in': 3600, 'scope': ' '.join(SCOPES)}
ME = {'sub': 'google-account-one', 'email': 'reader@example.com', 'email_verified': True}
REDIRECT = 'http://127.0.0.1:8765' + CALLBACK_PATH


@pytest.fixture(autouse=True)
def clean_deployment_env(monkeypatch):
    monkeypatch.delenv('WORKMATE_GOOGLE_CLIENT_ID', raising=False)
    monkeypatch.delenv('WORKMATE_GOOGLE_CLIENT_SECRET', raising=False)


def part(text, mime='text/plain', **extra):
    return {'mimeType': mime, 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}, **extra}


def mail(identity='ab123'):
    return {'id': identity, 'labelIds': ['INBOX', 'UNREAD'], 'internalDate': '1792715400000',
        'payload': {'mimeType': 'multipart/mixed', 'headers': [
            {'name': 'Subject', 'value': '정산 확인 요청'}, {'name': 'From', 'value': '발신자 <sender@example.com>'},
            {'name': 'To', 'value': 'reader@example.com'}, {'name': 'Date', 'value': 'Fri, 23 Oct 2026 09:30:00 +0900'}],
            'parts': [part('할 일: 정산 확인 | 담당: 김서연 | 기한: 2026-10-26'),
                      part('attachment private content', filename='details.txt'),
                      part('unnamed attachment content', headers=[{'name': 'Content-Disposition', 'value': 'attachment'}]),
                      {'mimeType': 'image/png', 'body': {'attachmentId': 'never-fetch-this'}}]}}


def event(**extra):
    return {'id': 'event-one', 'summary': '정산 협의', 'description': '<p>요청 내용 확인</p><script>secret()</script>',
            'start': {'dateTime': '2026-10-26T10:00:00+09:00'},
            'end': {'dateTime': '2026-10-26T11:00:00+09:00'}, 'status': 'confirmed',
            'organizer': {'email': 'organizer@example.com'},
            'attendees': [{'email': 'reader@example.com'}], **extra}


class Provider:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def handle(self, request):
        self.calls.append(request)
        url = str(request.url).split('?', 1)[0]
        default = TOKENS if url == TOKEN_URL else ME if url == USER_URL else {'messages': []} if url == MAIL_URL else {'items': [], 'timeZone': 'Asia/Seoul'}
        value = self.responses.get(url, (200, default, {}))
        if isinstance(value, list):
            value = value.pop(0)
        status, body, headers = value
        return httpx.Response(status, json=body, headers=headers)

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def setup(tmp_path):
    provider, stamp = Provider(), [1000.0]
    ctx = SimpleNamespace(store=Store(tmp_path / 'google.db'))
    app = FastAPI()
    install_google_workspace(app, ctx)
    ctx.google = GoogleWorkspaceConnector(ctx.store, REDIRECT, provider.factory, lambda: stamp[0])
    with TestClient(app) as client:
        yield client, ctx, provider, stamp
    ctx.google.close()


def start(client):
    saved = client.post('/api/google/config', json={'client_id': CLIENT_ID, 'client_secret': SECRET})
    assert saved.status_code == 200
    response = client.post('/api/google/auth/start')
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()['authorization_url']).query)
    return query


def connect(client):
    query = start(client)
    response = client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'private-auth-code'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/?google_auth=connected#connections'
    assert client.get('/api/google/status').json()['connected']
    return query


def test_authorization_code_pkce_state_secret_never_returned_or_persisted(setup):
    client, ctx, provider, _ = setup
    query = start(client)
    assert query['redirect_uri'] == [REDIRECT]
    assert query['response_type'] == ['code']
    assert set(query['scope'][0].split()) == set(SCOPES)
    assert query['code_challenge_method'] == ['S256']
    assert 'client_secret' not in query and 'code_verifier' not in query
    assert len(provider.calls) == 0
    response = client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'private-auth-code'}, follow_redirects=False)
    assert response.status_code == 303 and 'private' not in response.text
    assert response.headers['referrer-policy'] == 'no-referrer'
    form = parse_qs(provider.calls[0].content.decode())
    assert form['client_secret'] == [SECRET] and form['redirect_uri'] == [REDIRECT]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).decode().rstrip('=')
    assert challenge == query['code_challenge'][0]
    assert form['grant_type'] == ['authorization_code']
    assert str(provider.calls[1].url) == USER_URL
    status = client.get('/api/google/status').json()
    assert status['connected'] and status['account']['email'] == ME['email']
    assert status['read_only'] and status['client_secret_configured']
    public = json.dumps(status)
    assert not any(value in public for value in (SECRET, TOKENS['access_token'], TOKENS['refresh_token'], form['code_verifier'][0]))
    assert ctx.store.all('documents') == [] and ctx.store.all('connected_items') == []


def test_state_wrong_expired_unicode_and_replayed_never_exchange(setup):
    client, ctx, provider, stamp = setup
    query = start(client)
    for bad in ('not-the-state', '한글', 'a' * 43):
        assert client.get(CALLBACK_PATH, params={'state': bad, 'code': 'secret'}).status_code == 400
    assert provider.calls == [] and ctx.google._pending
    stamp[0] += 601
    assert client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'secret'}).status_code == 400
    assert provider.calls == []
    query = connect(client)
    count = len(provider.calls)
    assert client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'secret'}).status_code == 400
    assert len(provider.calls) == count


def test_callback_state_bound_to_one_connector(tmp_path):
    provider = Provider()
    a = GoogleWorkspaceConnector(Store(tmp_path / 'a.db'), REDIRECT, provider.factory)
    b = GoogleWorkspaceConnector(Store(tmp_path / 'b.db'), REDIRECT, provider.factory)
    a.configure(CLIENT_ID, SECRET)
    b.configure(CLIENT_ID, SECRET)
    state = parse_qs(urlsplit(a.start()['authorization_url']).query)['state'][0]
    with pytest.raises(HTTPException) as error:
        b.callback(state, 'authorization-code')
    assert error.value.status_code == 400 and not provider.calls
    assert not b.status()['connected'] and a._pending


def test_denied_callback_consumes_state_without_exchange(setup):
    client, ctx, provider, _ = setup
    query = start(client)
    response = client.get(CALLBACK_PATH, params={'state': query['state'][0], 'error': 'access_denied',
                          'error_description': 'private raw text'}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'].endswith('failed#connections')
    assert ctx.google.auth_status == 'denied' and not ctx.google._pending and provider.calls == []
    assert 'private' not in client.get('/api/google/status').text


def test_partial_scope_and_profile_failures_clear_tokens(setup):
    client, ctx, provider, _ = setup
    provider.responses[TOKEN_URL] = (200, {**TOKENS, 'scope': 'openid email'}, {})
    query = start(client)
    client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'code'}, follow_redirects=False)
    assert not ctx.google._token and not ctx.google.status()['connected']
    assert len(provider.calls) == 1
    provider.responses[TOKEN_URL] = (200, TOKENS, {})
    provider.responses[USER_URL] = (200, {**ME, 'email_verified': False}, {})
    query = start(client)
    client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'code'}, follow_redirects=False)
    assert not ctx.google._token and ctx.google.auth_status == 'error'


def test_provider_error_does_not_echo_credentials(setup):
    client, ctx, provider, _ = setup
    provider.responses[TOKEN_URL] = (400, {'error': 'invalid_client', 'error_description': SECRET + TOKENS['access_token']}, {})
    query = start(client)
    response = client.get(CALLBACK_PATH, params={'state': query['state'][0], 'code': 'code'}, follow_redirects=False)
    assert response.status_code == 303
    public = client.get('/api/google/status').text
    assert SECRET not in public and TOKENS['access_token'] not in public and not ctx.google._token


def test_refresh_after_expiry_and_definitive_401_clears_auth(setup):
    client, ctx, provider, stamp = setup
    connect(client)
    stamp[0] += 3601
    assert client.get('/api/google/messages?limit=1').status_code == 200
    forms = [parse_qs(call.content.decode()) for call in provider.calls if str(call.url) == TOKEN_URL]
    assert forms[-1]['grant_type'] == ['refresh_token']
    assert forms[-1]['refresh_token'] == [TOKENS['refresh_token']]
    provider.responses[MAIL_URL] = [(401, {'error': {}}, {}), (401, {'error': {}}, {})]
    assert client.get('/api/google/messages?limit=1').status_code == 401
    assert not ctx.google._token and not ctx.google._refresh and ctx.google.account is None
    assert ctx.google.auth_status == 'expired'


def test_refresh_failure_is_sanitized_and_clears_state(setup):
    client, ctx, provider, stamp = setup
    connect(client)
    stamp[0] += 3601
    provider.responses[TOKEN_URL] = (400, {'error': 'invalid_grant', 'error_description': TOKENS['refresh_token']}, {})
    response = client.get('/api/google/events')
    assert response.status_code == 401 and TOKENS['refresh_token'] not in response.text
    assert not ctx.google.status()['connected']


@pytest.mark.parametrize('status,body', [(429, {'error': {}}),
    (403, {'error': {'errors': [{'reason': 'userRateLimitExceeded'}]}})])
def test_rate_limit_honors_wait_without_more_network(setup, status, body):
    client, ctx, provider, stamp = setup
    connect(client)
    provider.responses[MAIL_URL] = (status, body, {'Retry-After': '12'})
    response = client.get('/api/google/messages')
    assert response.status_code == 429 and response.headers['retry-after'] == '12'
    count = len(provider.calls)
    assert client.get('/api/google/messages').status_code == 429 and len(provider.calls) == count
    stamp[0] += 12
    provider.responses[MAIL_URL] = (200, {'messages': []}, {})
    assert client.get('/api/google/messages').status_code == 200


def test_only_fixed_google_endpoints_and_get_data_allowed(setup):
    _, ctx, provider, _ = setup
    for method, url in [('GET', 'https://gmail.googleapis.com.evil.test/gmail/v1/users/me/messages'),
                        ('GET', MAIL_URL + '/../settings'), ('GET', MAIL_URL + '/ab1?token=x'),
                        ('POST', MAIL_URL), ('GET', 'http://oauth2.googleapis.com/token'),
                        ('POST', 'https://oauth2.googleapis.com/token/extra')]:
        with pytest.raises(HTTPException) as error:
            ctx.google._request(method, url)
        assert error.value.status_code == 400
    assert provider.calls == []


def test_mime_plain_preference_excludes_attachments_and_remote_html():
    raw = mail()
    raw['payload']['parts'].append(part('<p>HTML alternate</p><img src="https://evil.example/pixel"><script>bad()</script>', 'text/html'))
    result = normalize_message(raw)
    assert result['body'] == '할 일: 정산 확인 | 담당: 김서연 | 기한: 2026-10-26'
    assert result['sender'] == 'sender@example.com' and result['received_at'] and result['sent_at']
    assert not result['is_read']
    raw['payload']['parts'] = [part('<p>안건 확인</p><script>private()</script><img src="https://evil.example">', 'text/html')]
    result = normalize_message(raw)
    assert result['body'] == '안건 확인' and not result['web_link']


def test_invalid_body_encoding_and_unavailable_body_are_honest():
    raw = mail()
    raw['payload']['parts'] = [{'mimeType': 'text/plain', 'body': {'data': '***invalid***'}}]
    raw['snippet'] = '일부 미리보기'
    result = normalize_message(raw)
    assert result['body'] == '일부 미리보기' and len(result['warnings']) == 2
    raw['payload']['parts'] = [part('a' * 45000), part('b' * 45000)]
    result = normalize_message(raw)
    assert len(result['body']) == 80000 and any('발췌' in warning for warning in result['warnings'])


def test_calendar_all_day_uses_original_zone_and_exclusive_end():
    result = normalize_event(event(start={'date': '2026-11-01'}, end={'date': '2026-11-02'}), 'America/New_York')
    assert result['is_all_day'] and result['end_exclusive'] and not result['time_uncertain']
    assert result['start_at'] == '2026-11-01T00:00:00-04:00'
    assert result['end_at'] == '2026-11-02T00:00:00-05:00'
    assert datetime.fromisoformat(result['end_at']) - datetime.fromisoformat(result['start_at']) == timedelta(hours=25)
    assert result['original_start_date'] == '2026-11-01'


def test_calendar_uncertainty_recurrence_cancelled_free_and_links():
    raw = event(start={'dateTime': '2026-10-26T10:00:00'}, end={'dateTime': '2026-10-26T11:00:00'},
                status='cancelled', transparency='transparent', recurrence=['RRULE:FREQ=WEEKLY'],
                htmlLink='https://calendar.google.com.evil.test/private')
    result = normalize_event(raw, 'Unknown/TimeZone')
    assert result['time_uncertain'] and result['recurrence_unexpanded'] and result['warnings']
    assert result['status'] == 'cancelled' and result['show_as'] == 'free' and not result['web_link']
    assert 'secret' not in result['body']
    valid = normalize_event(event(recurringEventId='series-one'), 'Asia/Seoul')
    assert not valid['recurrence_unexpanded'] and valid['instance_type'] == 'occurrence'


def test_bounded_read_no_page_follow_selected_import_is_idempotent(setup):
    client, ctx, provider, _ = setup
    connect(client)
    provider.responses[MAIL_URL] = (200, {'messages': [{'id': 'ab123'}, {'id': 'ab456'}], 'nextPageToken': 'never-follow'}, {})
    provider.responses[MAIL_URL + '/ab123'] = (200, mail(), {})
    provider.responses[EVENTS_URL] = (200, {'items': [event()], 'timeZone': 'Asia/Seoul', 'nextPageToken': 'never-follow'}, {})
    response = client.get('/api/google/messages?limit=1').json()
    assert response['count'] == 1 and response['truncated']
    assert len(ctx.store.all('documents')) == 0
    assert not any('pageToken' in call.url.params for call in provider.calls)
    response = client.get('/api/google/events?days=14&limit=1').json()
    assert response['count'] == 1 and response['truncated']
    call = provider.calls[-1]
    assert call.url.params['singleEvents'] == 'true' and 'timeZone' not in call.url.params
    bad = client.post('/api/google/import', json={'message_ids': ['unseen'], 'project': 'ERP'})
    assert bad.status_code == 400
    imported = client.post('/api/google/import', json={'message_ids': ['ab123'], 'project': 'ERP'}).json()
    assert imported['created'] == 1 and imported['documents'][0]['source_type'] == 'email'
    assert imported['documents'][0]['project'] == 'ERP' and imported['items'][0]['provider'] == 'google'
    assert len(ctx.store.all('documents')) == 1  # Unselected event never persisted.
    repeated = client.post('/api/google/import', json={'message_ids': ['ab123'], 'project': 'ERP'}).json()
    assert repeated['created'] == 0 and repeated['existing'] == 1
    ctx.store.put('connected_items', {'id': 'microsoft-item', 'provider': 'm365', 'kind': 'event'})
    assert client.get('/api/google/status').json()['imported'] == {'messages': 1, 'events': 0, 'total': 1}


def test_gmail_invalid_id_does_not_become_url(setup):
    client, _, provider, _ = setup
    connect(client)
    provider.responses[MAIL_URL] = (200, {'messages': [{'id': '../../settings'}]}, {})
    response = client.get('/api/google/messages')
    assert response.status_code == 502
    assert str(provider.calls[-1].url).split('?', 1)[0] == MAIL_URL


def test_local_search_does_not_send_user_query_to_provider(setup):
    client, _, provider, _ = setup
    connect(client)
    provider.responses[MAIL_URL] = (200, {'messages': [{'id': 'ab123'}]}, {})
    provider.responses[MAIL_URL + '/ab123'] = (200, mail(), {})
    result = client.get('/api/google/messages?limit=1&q=정산').json()
    assert result['count'] == 1
    assert all('q' not in call.url.params for call in provider.calls)


def test_config_disconnect_and_close_clear_secrets_at_right_lifecycle(setup):
    client, ctx, _, _ = setup
    connect(client)
    result = client.delete('/api/google/connection').json()
    assert result['configured'] and not result['connected'] and ctx.google._secret == SECRET
    assert ctx.google._pending is None and not ctx.google.messages and not ctx.google.events
    connect(client)
    changed = client.post('/api/google/config', json={'client_id': CLIENT_ID, 'client_secret': 'GOCSPX-another-own-secret'}).json()
    assert not changed['connected'] and ctx.google.account is None
    ctx.google.close()
    assert not ctx.google._secret and not ctx.google.client_id and not ctx.google.status()['configured']


def test_invalid_config_limits_and_fixed_callback_origin(setup):
    client, _, provider, _ = setup
    for identity, secret in [('https://evil.example/app', SECRET), (CLIENT_ID, 'bad'), (CLIENT_ID, SECRET + '\nINJECT')]:
        response = client.post('/api/google/config', json={'client_id': identity, 'client_secret': secret})
        assert response.status_code == 400 and secret not in response.text
    assert client.get('/api/google/messages?limit=26').status_code == 422
    assert client.get('/api/google/events?days=365').status_code == 400
    assert not provider.calls
    assert callback_uri(SimpleNamespace(HOSTED=True, PUBLIC_HOST='workmate-otpo.onrender.com')) == 'https://workmate-otpo.onrender.com' + CALLBACK_PATH
    with pytest.raises(ValueError):
        callback_uri(SimpleNamespace(HOSTED=True, PUBLIC_HOST='evil.example/path'))
    with pytest.raises(ValueError):
        GoogleWorkspaceConnector(None, 'https://example.com/anything?redirect=evil')


def test_response_size_limit_and_redirect_are_not_followed(setup):
    _, ctx, provider, _ = setup
    provider.responses[TOKEN_URL] = (302, {}, {'Location': 'https://evil.example'})
    with pytest.raises(HTTPException) as error:
        ctx.google._request('POST', TOKEN_URL)
    assert error.value.status_code == 502 and len(provider.calls) == 1
    provider.responses[TOKEN_URL] = (200, {'oversized': 'x' * MAX_RESPONSE}, {})
    with pytest.raises(HTTPException) as error:
        ctx.google._request('POST', TOKEN_URL)
    assert error.value.status_code == 502


def test_callback_removes_query_from_asgi_scope_before_response(tmp_path):
    ctx = SimpleNamespace(store=Store(tmp_path / 'scope.db'))
    app, provider, observed = FastAPI(), Provider(), []
    install_google_workspace(app, ctx)
    ctx.google = GoogleWorkspaceConnector(ctx.store, REDIRECT, provider.factory)
    @app.middleware('http')
    async def observe(request, call_next):
        response = await call_next(request)
        if request.url.path == CALLBACK_PATH:
            observed.append(request.scope['query_string'])
        return response
    with TestClient(app) as client:
        connect(client)
    assert observed == [b'']


def test_hosted_without_public_host_keeps_app_available_but_oauth_disabled(tmp_path):
    ctx = SimpleNamespace(store=Store(tmp_path / 'no-host.db'), HOSTED=True, PUBLIC_HOST='')
    app = FastAPI()
    install_google_workspace(app, ctx)
    with TestClient(app) as client:
        response = client.get('/api/google/status')
        assert response.status_code == 200
        status = response.json()
        assert status['redirect_uri'] == '' and status['configuration_error'] and not status['configured']
        assert client.post('/api/google/config', json={'client_id': CLIENT_ID, 'client_secret': SECRET}).status_code == 400
        assert client.post('/api/google/auth/start').status_code == 400
        assert client.get(CALLBACK_PATH, params={'state': 'x' * 43, 'code': 'code'}).status_code == 400
        assert not ctx.google._secret and not ctx.google._pending


def test_deployment_app_credentials_do_not_share_visitor_auth(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKMATE_GOOGLE_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('WORKMATE_GOOGLE_CLIENT_SECRET', SECRET)
    provider = Provider()
    a, b = SimpleNamespace(store=Store(tmp_path / 'env-a.db')), SimpleNamespace(store=Store(tmp_path / 'env-b.db'))
    for ctx in (a, b):
        install_google_workspace(FastAPI(), ctx)
        ctx.google._factory = provider.factory
        status = ctx.google.status()
        assert status['configured'] and status['configuration_source'] == 'deployment'
        assert not status['connected'] and SECRET not in json.dumps(status)
    state = parse_qs(urlsplit(a.google.start()['authorization_url']).query)['state'][0]
    assert not b.google._pending
    assert a.google.callback(state, 'code')
    assert a.google.status()['connected'] and not b.google.status()['connected']
    assert not b.google._token and not b.google._refresh and b.google.account is None
    a.google.disconnect()
    assert a.google.status()['configured'] and a.google.status()['configuration_source'] == 'deployment'
    a.google.configure(CLIENT_ID, 'GOCSPX-visitor-only-secret')
    assert a.google.status()['configuration_source'] == 'workspace' and b.google._secret == SECRET
    assert not a.store.all('documents') and not b.store.all('documents')


@pytest.mark.parametrize('identity,secret', [(CLIENT_ID, ''), ('', SECRET), ('invalid-client', SECRET), (CLIENT_ID, 'bad')])
def test_incomplete_or_invalid_deployment_config_does_not_crash_app(tmp_path, monkeypatch, identity, secret):
    monkeypatch.setenv('WORKMATE_GOOGLE_CLIENT_ID', identity)
    monkeypatch.setenv('WORKMATE_GOOGLE_CLIENT_SECRET', secret)
    app, ctx = FastAPI(), SimpleNamespace(store=Store(tmp_path / 'invalid-env.db'))
    install_google_workspace(app, ctx)
    with TestClient(app) as client:
        response = client.get('/api/google/status')
        assert response.status_code == 200
        status = response.json()
        assert not status['configured'] and not status['connected'] and status['configuration_error']
        assert SECRET not in response.text
        assert client.post('/api/google/auth/start').status_code == 400
        # A visitor may still use their own valid app settings.
        configured = client.post('/api/google/config', json={'client_id': CLIENT_ID, 'client_secret': SECRET}).json()
        assert configured['configured'] and configured['configuration_error'] is None
        assert configured['configuration_source'] == 'workspace'
