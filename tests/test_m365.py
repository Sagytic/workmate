import json
from email.message import EmailMessage
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.m365 import (MAX_FILE, M365Connector, _graph_event, import_items,
                          install_m365, parse_eml, parse_ics, plain_html, safe_link)
from backend.store import Store

CLIENT_ID = '12345678-1234-1234-1234-123456789abc'
DEVICE = {'user_code': 'READ-CODE', 'device_code': 'private-device-token', 'verification_uri': 'https://microsoft.com/devicelogin', 'expires_in': 900, 'interval': 5}
TOKENS = {'access_token': 'private-access-token', 'refresh_token': 'private-refresh-token', 'token_type': 'Bearer', 'expires_in': 3600}
ME = {'id': 'account-one', 'displayName': '김지민', 'mail': 'reader@example.com'}


class Provider:
    def __init__(self):
        self.calls = []
        self.device = DEVICE.copy()
        self.token_responses = []
        self.graph = {}

    def handle(self, request):
        self.calls.append(request)
        if request.url.path.endswith('/devicecode'):
            return httpx.Response(200, json=self.device)
        if request.url.path.endswith('/token'):
            status, body, headers = self.token_responses.pop(0) if self.token_responses else (200, TOKENS, {})
            return httpx.Response(status, json=body, headers=headers)
        path = request.url.path.removeprefix('/v1.0')
        response = self.graph.get(path, (200, ME if path == '/me' else {'value': []}, {}))
        if isinstance(response, list):
            response = response.pop(0)
        status, body, headers = response
        return httpx.Response(status, json=body, headers=headers)

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def fixture(tmp_path):
    store, provider, stamp = Store(tmp_path / 'm365.db'), Provider(), [1000.0]
    ctx = SimpleNamespace(store=store)
    app = FastAPI()
    install_m365(app, ctx)
    ctx.m365 = M365Connector(store, provider.factory, lambda: stamp[0])
    with TestClient(app) as client:
        yield client, ctx, provider, stamp
    ctx.m365.close()


def connect(client, stamp):
    assert client.post('/api/m365/config', json={'client_id': CLIENT_ID}).status_code == 200
    assert client.post('/api/m365/auth/start').status_code == 200
    stamp[0] += 5
    response = client.post('/api/m365/auth/poll')
    assert response.status_code == 200, response.text
    assert response.json()['connected']
    return response.json()


def event_file(properties):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:meeting-1\r\n' + properties + '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode('utf-8')


def message_file():
    mail = EmailMessage()
    mail['Subject'] = '정산 협의 요청'
    mail['From'] = '김지민 <sender@example.com>'
    mail['To'] = '박서연 <reviewer@example.com>'
    mail['Date'] = 'Fri, 23 Oct 2026 09:30:00 +0900'
    mail['Message-ID'] = '<mail-one@example.com>'
    mail.set_content('결정: 월말 마감을 2단계로 진행합니다.\n할 일: 숫자 검토 | 담당: 박서연')
    return mail


def test_device_flow_never_returns_or_persists_secret_and_waits_interval(fixture):
    client, ctx, provider, stamp = fixture
    configured = client.post('/api/m365/config', json={'client_id': CLIENT_ID}).json()
    assert configured['configured'] and not configured['connected']
    first = client.post('/api/m365/auth/start').json()
    assert first['user_code'] == 'READ-CODE' and 'device_code' not in first
    assert client.post('/api/m365/auth/poll').json()['auth']['next_poll_in'] == 5
    assert len(provider.calls) == 1
    provider.token_responses = [(400, {'error': 'authorization_pending'}, {}), (400, {'error': 'slow_down'}, {})]
    stamp[0] += 5
    assert client.post('/api/m365/auth/poll').json()['auth']['status'] == 'pending'
    stamp[0] += 5
    slow = client.post('/api/m365/auth/poll').json()
    assert slow['auth']['interval'] == 10 and slow['auth']['next_poll_in'] == 10
    stamp[0] += 10
    connected = client.post('/api/m365/auth/poll').json()
    assert connected['account']['email'] == ME['mail']
    assert connected['read_only'] and connected['scopes'] == ['User.Read', 'Mail.Read', 'Calendars.Read']
    public = json.dumps([first, configured, slow, connected, client.get('/api/m365/status').json()])
    for value in ('private-device-token', 'private-access-token', 'private-refresh-token'):
        assert value not in public and value.encode() not in ctx.store.path.read_bytes()
    sent = parse_qs(provider.calls[0].content.decode())['scope'][0]
    assert 'Mail.Read ' in sent and 'Calendars.Read' in sent and 'offline_access' in sent and 'Write' not in sent


def test_expired_and_denied_auth_do_not_become_connected(fixture):
    client, ctx, provider, stamp = fixture
    client.post('/api/m365/config', json={'client_id': CLIENT_ID})
    client.post('/api/m365/auth/start')
    stamp[0] += 901
    assert client.post('/api/m365/auth/poll').json()['auth']['status'] == 'expired'
    assert len(provider.calls) == 1
    client.post('/api/m365/auth/start')
    provider.token_responses = [(400, {'error': 'authorization_declined', 'error_description': 'private-device-token'}, {})]
    stamp[0] += 5
    response = client.post('/api/m365/auth/poll')
    assert response.json()['auth']['status'] == 'denied' and not response.json()['connected']
    assert 'private-device-token' not in response.text


@pytest.mark.parametrize('tenant', ['https://evil.example', '../common', 'org@evil.example', 'org/example.com', 'a..com', 'example.com?x=1', 'foo\\bar'])
def test_tenant_guard_rejects_address_injection(fixture, tenant):
    client, ctx, provider, stamp = fixture
    assert client.post('/api/m365/config', json={'client_id': CLIENT_ID, 'tenant': tenant}).status_code == 400
    assert not provider.calls


def test_client_and_network_origin_guards(fixture):
    client, ctx, provider, stamp = fixture
    assert client.post('/api/m365/config', json={'client_id': 'not-a-guid'}).status_code == 400
    for tenant in ('common', 'consumers', 'tenant.onmicrosoft.com', CLIENT_ID):
        assert client.post('/api/m365/config', json={'client_id': CLIENT_ID, 'tenant': tenant}).status_code == 200
    with pytest.raises(HTTPException):
        ctx.m365._request('GET', 'https://graph.microsoft.com.evil.example/v1.0/me')
    with pytest.raises(HTTPException):
        ctx.m365._graph('https://evil.example/me')
    provider.device['verification_uri'] = 'https://microsoft.com.evil.example/devicelogin'
    assert client.post('/api/m365/auth/start').status_code == 502
    assert not ctx.m365._token


def test_disconnect_and_config_change_clear_token_and_selection(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    ctx.m365.messages['old'] = {'id': 'old'}
    response = client.delete('/api/m365/connection').json()
    assert response['configured'] and not response['connected'] and not ctx.m365.messages
    assert not ctx.m365._token and not ctx.m365._refresh
    connect(client, stamp)
    response = client.post('/api/m365/config', json={'client_id': CLIENT_ID, 'tenant': 'common'}).json()
    assert not response['connected'] and ctx.m365._pending is None


def test_refresh_and_graph_retry_then_permission_errors_are_sanitized(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    stamp[0] += 3590
    provider.token_responses = [(200, {**TOKENS, 'access_token': 'refreshed-secret'}, {})]
    assert client.get('/api/m365/messages').status_code == 200
    token_calls = [r for r in provider.calls if r.url.path.endswith('/token')]
    assert parse_qs(token_calls[-1].content.decode())['grant_type'] == ['refresh_token']
    assert provider.calls[-1].headers['authorization'] == 'Bearer refreshed-secret'
    provider.graph['/me/mailFolders/inbox/messages'] = [(401, {'error': {'message': 'refreshed-secret'}}, {}), (403, {'error': {'message': 'private-refresh-token'}}, {})]
    response = client.get('/api/m365/messages')
    assert response.status_code == 403
    assert 'secret' not in response.text and 'private-refresh' not in response.text


def test_rate_limit_is_observed_without_repeat_provider_calls(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    provider.graph['/me/calendarView'] = (429, {'error': {'message': 'raw secret'}}, {'Retry-After': '12'})
    response = client.get('/api/m365/events')
    assert response.status_code == 429 and response.headers['Retry-After'] == '12'
    count = len(provider.calls)
    assert client.get('/api/m365/events').status_code == 429 and len(provider.calls) == count
    stamp[0] += 12
    provider.graph['/me/calendarView'] = (200, {'value': []}, {})
    assert client.get('/api/m365/events').status_code == 200


def test_second_unauthorized_response_clears_connected_state(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    provider.graph['/me/mailFolders/inbox/messages'] = (401, {'error': {'message': 'private-access-token'}}, {})
    assert client.get('/api/m365/messages').status_code == 401
    status = client.get('/api/m365/status').json()
    assert not status['connected'] and status['auth']['status'] == 'expired'
    assert not ctx.m365._token and not ctx.m365._refresh and ctx.m365.account is None


def test_account_verification_failure_discards_tokens(fixture):
    client, ctx, provider, stamp = fixture
    client.post('/api/m365/config', json={'client_id': CLIENT_ID})
    client.post('/api/m365/auth/start')
    provider.graph['/me'] = (403, {'error': {'message': TOKENS['access_token']}}, {})
    stamp[0] += 5
    response = client.post('/api/m365/auth/poll')
    assert response.status_code == 403 and not ctx.m365._token and not ctx.m365._refresh
    assert client.get('/api/m365/status').json()['auth']['status'] == 'error'


def test_graph_reads_are_bounded_and_selected_import_is_versioned_idempotent(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    provider.graph['/me/mailFolders/inbox/messages'] = (200, {'value': [
        {'id': 'm1', 'subject': '계약 확인', 'body': {'contentType': 'html', 'content': '<p>검토 요청</p><script>bad()</script>'},
         'from': {'emailAddress': {'name': '김지민', 'address': 'sender@example.com'}}, 'receivedDateTime': '2026-10-23T00:00:00Z', 'webLink': 'https://outlook.office.com/mail/item/m1'},
        {'id': 'm2', 'subject': '다른 내용', 'body': {'contentType': 'text', 'content': '아직 선택하지 않은 내용'}}],
        '@odata.nextLink': 'https://evil.example/steal'}, {})
    response = client.get('/api/m365/messages?limit=1').json()
    assert response['count'] == 1 and response['truncated']
    assert 'bad()' not in response['messages'][0]['body']
    assert provider.calls[-1].url.params['$top'] == '1'
    assert not ctx.store.all('documents')
    assert client.post('/api/m365/import', json={'message_ids': ['m2']}).status_code == 400
    first = client.post('/api/m365/import', json={'message_ids': ['m1', 'm1'], 'project': 'ERP'}).json()
    second = client.post('/api/m365/import', json={'message_ids': ['m1'], 'project': 'ERP'}).json()
    assert first['created'] == 1 and second['existing'] == 1
    doc = first['documents'][0]
    assert doc['source_type'] == 'email' and '검토 요청' in doc['body'] and doc['project'] == 'ERP'
    ctx.store.update('documents', doc['id'], body='사용자가 편집한 자료')
    assert client.post('/api/m365/import', json={'message_ids': ['m1'], 'project': 'ERP'}).json()['documents'][0]['body'] == '사용자가 편집한 자료'
    ctx.m365.messages['m1']['body'] = '변경된 원문'
    third = client.post('/api/m365/import', json={'message_ids': ['m1'], 'project': 'ERP'}).json()
    assert third['created'] == 1 and third['documents'][0]['id'] != doc['id']
    assert client.get('/api/m365/messages?limit=500').status_code == 422
    assert client.get('/api/m365/events?days=500').status_code == 400
    assert all(r.url.host in ('login.microsoftonline.com', 'graph.microsoft.com') for r in provider.calls)


def test_graph_calendar_selects_expanded_window_and_safe_metadata(fixture):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    provider.graph['/me/calendarView'] = (200, {'value': [{
        'id': 'e1', 'subject': '고객 회의', 'body': {'contentType': 'text', 'content': '검수 기준 협의'},
        'start': {'dateTime': '2026-10-23T01:00:00.0000000', 'timeZone': 'UTC'},
        'end': {'dateTime': '2026-10-23T02:00:00.0000000', 'timeZone': 'UTC'}, 'type': 'occurrence',
        'showAs': 'busy', 'isCancelled': False, 'webLink': 'javascript:alert(1)'}]}, {})
    event = client.get('/api/m365/events?days=14').json()['events'][0]
    assert event['start_at'] == '2026-10-23T01:00:00+00:00' and not event['time_uncertain']
    assert not event['recurrence_unexpanded'] and event['web_link'] == ''
    imported = client.post('/api/m365/import', json={'event_ids': ['e1']}).json()['documents'][0]
    assert imported['source_metadata']['start_at'] == event['start_at'] and imported['source_type'] == 'event'


def test_graph_all_day_preserves_converted_instants_instead_of_wrong_date():
    event = _graph_event({'id': 'all-day-kst', 'subject': '종일 부재', 'isAllDay': True,
        'start': {'dateTime': '2026-10-25T15:00:00', 'timeZone': 'UTC'},
        'end': {'dateTime': '2026-10-26T15:00:00', 'timeZone': 'UTC'}})
    assert event['is_all_day'] and not event['time_uncertain']
    assert event['start_at'] == '2026-10-25T15:00:00+00:00'
    assert event['end_at'] == '2026-10-26T15:00:00+00:00'
    from datetime import datetime
    from backend.store import KST
    assert datetime.fromisoformat(event['start_at']).astimezone(KST).isoformat() == '2026-10-26T00:00:00+09:00'
    assert datetime.fromisoformat(event['end_at']).astimezone(KST).isoformat() == '2026-10-27T00:00:00+09:00'


def test_workspace_connectors_never_share_tokens_or_cached_ids(fixture, tmp_path):
    client, ctx, provider, stamp = fixture
    connect(client, stamp)
    other = M365Connector(Store(tmp_path / 'second.db'), provider.factory, lambda: stamp[0])
    other.configure(CLIENT_ID, 'organizations')
    assert not other.status()['connected'] and other.status()['account'] is None
    with pytest.raises(HTTPException) as error:
        other.read_messages()
    assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        other.import_selected(['cached-in-another-workspace'], [], '')
    ctx.m365.close()
    assert not ctx.m365._token and not ctx.m365._refresh


def test_eml_decodes_headers_excludes_attachments_and_normalizes_html(fixture):
    client, ctx, provider, stamp = fixture
    mail = message_file()
    mail.add_alternative('<p>HTML 대체 본문</p><img src="https://evil.example/pixel"><script>secret()</script>', subtype='html')
    mail.add_attachment(b'private attachment body', maintype='text', subtype='plain', filename='secret.txt')
    data = mail.as_bytes()
    parsed = parse_eml(data)[0]
    assert parsed['subject'] == '정산 협의 요청' and parsed['sender'] == 'sender@example.com'
    assert parsed['sent_at'] == '2026-10-23T09:30:00+09:00' and parsed['received_at'] is None
    assert '결정:' in parsed['body'] and 'private attachment' not in parsed['body'] and 'HTML 대체' not in parsed['body']
    response = client.post('/api/m365/files', files={'file': ('mail.eml', data, 'message/rfc822')}, data={'project': 'ERP'})
    assert response.status_code == 200 and response.json()['created'] == 1
    assert client.post('/api/m365/files', files={'file': ('mail.eml', data)} , data={'project': 'ERP'}).json()['existing'] == 1
    assert not provider.calls
    assert plain_html('<head>hidden</head><p>Hello&nbsp;world</p><style>bad</style><iframe src=x>evil</iframe><div>Next</div>') == 'Hello\xa0world\n\nNext'


def test_eml_received_header_is_separate_from_authored_date():
    mail = message_file()
    mail['Received'] = 'from sender.example by mail.example; Fri, 23 Oct 2026 09:32:15 +0900'
    item = parse_eml(mail.as_bytes())[0]
    assert item['sent_at'] == '2026-10-23T09:30:00+09:00'
    assert item['received_at'] == '2026-10-23T09:32:15+09:00' and item['date_source'] == 'Received'


def test_ics_unfolding_text_timezone_all_day_and_end_exclusivity():
    data = event_file('SUMMARY:프로젝트 회의\r\nDESCRIPTION:첫 줄\\n둘째 줄\\, 확인\r\n 계속\r\nDTSTART;TZID=Asia/Seoul:20261023T100000\r\nDTEND;TZID=Asia/Seoul:20261023T110000\r\nORGANIZER:mailto:owner@example.com\r\nATTENDEE;CN="박서연":mailto:reviewer@example.com')
    item = parse_ics(data)[0]
    assert item['start_at'] == '2026-10-23T10:00:00+09:00' and not item['time_uncertain']
    assert item['body'] == '첫 줄\n둘째 줄, 확인계속' and item['attendees'] == ['reviewer@example.com']
    day = parse_ics(event_file('SUMMARY:휴가\r\nDTSTART;VALUE=DATE:20261023\r\nDTEND;VALUE=DATE:20261025'))[0]
    assert day['start_at'] == '2026-10-23' and day['end_at'] == '2026-10-25' and day['end_exclusive'] and day['is_all_day']
    single = parse_ics(event_file('DTSTART;VALUE=DATE:20261023'))[0]
    assert single['end_at'] == '2026-10-24' and single['warnings']


def test_ics_unknown_floating_dst_and_recurrence_are_not_free_time():
    floating = parse_ics(event_file('DTSTART:20261023T100000\r\nDTEND:20261023T110000\r\nRRULE:FREQ=WEEKLY;COUNT=4'))[0]
    assert floating['time_uncertain'] and floating['recurrence_unexpanded'] and len(floating['warnings']) == 2
    assert floating['start_at'] == '2026-10-23T10:00:00'
    custom = parse_ics(event_file('DTSTART;TZID=Custom/Company:20261023T100000\r\nDTEND;TZID=Custom/Company:20261023T110000'))[0]
    assert custom['time_uncertain'] and custom['time_zone'] == 'Custom/Company'
    ambiguous = parse_ics(event_file('DTSTART;TZID=America/New_York:20261101T013000\r\nDTEND;TZID=America/New_York:20261101T023000'))[0]
    assert ambiguous['time_uncertain']
    summer = parse_ics(event_file('DTSTART;TZID=America/New_York:20260701T100000\r\nDTEND;TZID=America/New_York:20260701T110000'))[0]
    assert summer['start_at'].endswith('-04:00') and not summer['time_uncertain']


def test_ics_duration_respects_dst_calendar_days_and_elapsed_hours():
    item = parse_ics(event_file('DTSTART;TZID=America/New_York:20261031T100000\r\nDURATION:P1D'))[0]
    assert item['start_at'].endswith('-04:00') and item['end_at'] == '2026-11-01T10:00:00-05:00'
    item = parse_ics(event_file('DTSTART;TZID=America/New_York:20261101T003000\r\nDURATION:PT3H'))[0]
    assert item['end_at'] == '2026-11-01T02:30:00-05:00'


def test_ics_cancelled_and_safe_web_link():
    item = parse_ics(event_file('DTSTART:20261023T010000Z\r\nDTEND:20261023T020000Z\r\nSTATUS:CANCELLED\r\nTRANSP:TRANSPARENT'))[0]
    assert item['status'] == 'cancelled' and item['show_as'] == 'free'
    assert not safe_link('https://outlook.office.com.evil.example/mail')
    assert not safe_link('https://user:pass@outlook.office.com/mail')
    assert safe_link('https://outlook.office.com/mail/id')


def test_file_and_event_bounds_reject_invalid_input_before_persisting(fixture):
    client, ctx, provider, stamp = fixture
    for filename, data in [('x.exe', b'x'), ('x.eml', b'x' * (MAX_FILE + 1)), ('x.ics', b'not a calendar'), ('x.eml', b'no headers')]:
        assert client.post('/api/m365/files', files={'file': (filename, data)}).status_code == 400
    for props in ['DTSTART:bad', 'DTSTART;VALUE=DATE:20261345', 'DTSTART:20261023T010000Z\r\nDTEND:20261023T000000Z', 'DTSTART;VALUE=DATE:20261023\r\nDTEND:20261024T010000Z', 'DTSTART;VALUE=DATE:99991231']:
        response = client.post('/api/m365/files', files={'file': ('calendar.ics', event_file(props))})
        assert response.status_code == 400, response.text
    assert not ctx.store.all('documents') and not ctx.store.all('connected_items')
    assert client.post('/api/m365/import', json={'message_ids': ['../other']}).status_code == 400
    assert client.get('/api/m365/examples/not-allowed').status_code == 404
