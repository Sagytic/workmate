"""Visitor-scoped, read-only Gmail and Google Calendar authorization.

Only explicit selections enter Store. OAuth configuration, PKCE verifiers and
tokens remain in this connector's memory. No send, draft-save or calendar-write
endpoint exists. The callback must arrive with the same visitor cookie.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .m365 import MAX_BODY, _local_time, clean, import_items, plain_html


SCOPES = ['openid', 'email', 'https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/calendar.events.readonly']
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USER_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
MAIL_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages'
EVENTS_URL = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
CALLBACK_PATH = '/api/google/auth/callback'
MAX_RESPONSE = 4 * 1024 * 1024


def callback_uri(ctx):
    if getattr(ctx, 'HOSTED', False):
        host = getattr(ctx, 'PUBLIC_HOST', '')
        if not host:
            # A deployment without a public hostname can still serve the other
            # tools. Never infer an OAuth origin from an untrusted Host header.
            return ''
        if not re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', host) or '..' in host:
            raise ValueError('Google 연결의 공개 호스트 설정을 확인하세요.')
        return 'https://' + host.lower() + CALLBACK_PATH
    return 'http://127.0.0.1:8765' + CALLBACK_PATH


def _valid_redirect(value):
    parsed = urlsplit(value)
    return (parsed.path == CALLBACK_PATH and not parsed.query and not parsed.fragment
            and not parsed.username and not parsed.password
            and ((parsed.scheme == 'https' and parsed.hostname and parsed.port in (None, 443))
                 or (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port == 8765)))


def _header(value):
    try:
        return clean(str(make_header(decode_header(str(value or '')))), 2000)
    except (ValueError, LookupError, UnicodeError):
        return clean(value, 2000)


def _mail_date(value):
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _google_link(value):
    value = clean(value, 2000)
    try:
        parts = urlsplit(value)
        if parts.scheme == 'https' and parts.hostname in ('calendar.google.com', 'www.google.com', 'mail.google.com') and not parts.username and not parts.password and parts.port in (None, 443):
            return value
    except ValueError:
        pass
    return ''


def normalize_message(raw):
    """Decode inline text parts only; never fetch attachmentId or remote HTML."""
    payload = raw.get('payload') if isinstance(raw.get('payload'), dict) else {}
    headers = {str(h.get('name', '')).lower(): _header(h.get('value'))
               for h in (payload.get('headers') or [])[:100] if isinstance(h, dict)}
    plain, rich, warnings, visited = [], [], [], [0]

    def walk(part, depth=0):
        visited[0] += 1
        if depth > 20 or visited[0] > 200:
            warnings.append('메일 구조가 길어 일부 본문을 읽지 못했습니다.')
            return
        if not isinstance(part, dict) or part.get('filename'):
            return
        dispositions = [str(h.get('value', '')) for h in (part.get('headers') or [])[:100]
                        if isinstance(h, dict) and str(h.get('name', '')).lower() == 'content-disposition']
        if any(value.lower().split(';', 1)[0].strip() == 'attachment' for value in dispositions):
            return
        mime = str(part.get('mimeType', '')).lower()
        if mime == 'message/rfc822':
            return
        if mime.startswith('multipart/'):
            for child in (part.get('parts') or [])[:200]:
                walk(child, depth + 1)
            return
        if mime not in ('text/plain', 'text/html'):
            return
        body = part.get('body') if isinstance(part.get('body'), dict) else {}
        encoded = body.get('data')
        if not isinstance(encoded, str) or not encoded:
            if body.get('attachmentId'):
                warnings.append('별도 파일로 제공되는 본문은 가져오지 않았습니다.')
            return
        if len(encoded) > MAX_BODY * 4 // 3:
            encoded = encoded[:(MAX_BODY * 4 // 3) // 4 * 4]
            warnings.append('본문 길이 제한으로 일부를 발췌했습니다.')
        try:
            decoded = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
            part_headers = {str(h.get('name', '')).lower(): str(h.get('value', ''))
                            for h in (part.get('headers') or [])[:100] if isinstance(h, dict)}
            message = EmailMessage()
            message['Content-Type'] = part_headers.get('content-type', mime)
            charset = message.get_content_charset() or 'utf-8'
            try:
                text = decoded.decode(charset, errors='replace')
            except LookupError:
                text = decoded.decode('utf-8', errors='replace')
            (plain if mime == 'text/plain' else rich).append(text)
        except (ValueError, TypeError):
            warnings.append('메일 본문 인코딩 일부를 읽지 못했습니다.')

    walk(payload)
    joined = '\n\n'.join(plain if plain else rich)
    if len(joined) > MAX_BODY:
        warnings.append('본문 길이 제한으로 일부를 발췌했습니다.')
    body = clean(joined) if plain else plain_html(joined)
    if not body:
        body = plain_html(raw.get('snippet', ''))
        warnings.append('본문 대신 Google이 제공한 미리보기만 포함했습니다.')
    sender = getaddresses([headers.get('from', '')])
    recipients = getaddresses([headers.get('to', '')])[:100]
    received = None
    try:
        received = datetime.fromtimestamp(int(raw['internalDate']) / 1000, timezone.utc).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        warnings.append('Gmail에 기록된 수신 시각을 확인하지 못했습니다.')
    identity = clean(raw.get('id'), 128)
    return {'id': identity, 'kind': 'email', 'provider': 'google',
            'subject': clean(headers.get('subject'), 300) or '제목 없는 메일', 'body': body,
            'from': headers.get('from', ''), 'sender': sender[0][1] if sender else '',
            'to': [f'{name} <{address}>' if name else address for name, address in recipients],
            'recipients': [address for _, address in recipients], 'received_at': received,
            'sent_at': _mail_date(headers.get('date', '')), 'date_source': 'Gmail internalDate',
            'is_read': 'UNREAD' not in (raw.get('labelIds') or []),
            'web_url': '', 'web_link': '', 'warnings': list(dict.fromkeys(warnings))}


def normalize_event(raw, calendar_zone):
    start = raw.get('start') if isinstance(raw.get('start'), dict) else {}
    end = raw.get('end') if isinstance(raw.get('end'), dict) else {}
    all_day = bool(start.get('date'))
    zone = start.get('timeZone') or calendar_zone or ''
    warnings, first, last, uncertain = [], None, None, True
    try:
        if all_day:
            # Calendar DATE belongs to that calendar's zone, not necessarily KST.
            # Preserve the original labels too; focus uses the verified instants.
            left, right = date.fromisoformat(start['date']), date.fromisoformat(end['date'])
            first, u1 = _local_time(left.isoformat() + 'T00:00:00', zone)
            last, u2 = _local_time(right.isoformat() + 'T00:00:00', zone)
        else:
            first, u1 = _local_time(start['dateTime'], zone)
            last, u2 = _local_time(end['dateTime'], end.get('timeZone') or calendar_zone or zone)
        a, b = datetime.fromisoformat(first), datetime.fromisoformat(last)
        uncertain = u1 or u2 or b <= a
    except (ValueError, TypeError, KeyError):
        uncertain = True
    if uncertain:
        warnings.append('일정의 시간대 또는 시작·종료를 확인해야 합니다.')
    recurrence = raw.get('recurrence') or []
    if recurrence:
        warnings.append('반복 일정 원형은 모두 펼쳐진 것으로 판단하지 않습니다.')
    organizer = raw.get('organizer') if isinstance(raw.get('organizer'), dict) else {}
    attendees = [clean(a.get('email'), 254) for a in (raw.get('attendees') or [])[:100] if isinstance(a, dict)]
    link = _google_link(raw.get('htmlLink'))
    return {'id': clean(raw.get('id'), 1024), 'kind': 'event', 'provider': 'google',
            'subject': clean(raw.get('summary'), 300) or '제목 없는 일정',
            'body': plain_html(raw.get('description', '')), 'start_at': first, 'end_at': last,
            'time_zone': zone, 'is_all_day': all_day, 'end_exclusive': True, 'time_uncertain': uncertain,
            'original_start_date': start.get('date'), 'original_end_date': end.get('date'),
            'location': clean(raw.get('location'), 1000), 'organizer': clean(organizer.get('email'), 254),
            'attendees': attendees, 'status': 'cancelled' if raw.get('status') == 'cancelled' else 'confirmed',
            'show_as': 'free' if raw.get('transparency') == 'transparent' else 'busy',
            'recurrence': recurrence, 'recurrence_unexpanded': bool(recurrence),
            'instance_type': 'occurrence' if raw.get('recurringEventId') else 'single',
            'web_url': link, 'web_link': link, 'warnings': warnings}


class GoogleWorkspaceConnector:
    def __init__(self, store, redirect_uri, client_factory=None, clock=None):
        if redirect_uri and not _valid_redirect(redirect_uri):
            raise ValueError('Google callback 주소 설정을 확인하세요.')
        self.store, self.redirect_uri = store, redirect_uri
        self._origin_error = '' if redirect_uri else 'Google 연결을 사용하려면 서비스의 공개 호스트를 설정해야 합니다.'
        self.configuration_error = self._origin_error
        self.configuration_source = None
        self._factory = client_factory or (lambda: httpx.Client(timeout=25, verify=ssl.create_default_context(), follow_redirects=False))
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.client_id, self._secret = '', ''
        self._token, self._refresh = '', ''
        self._expires, self._blocked_until = 0, 0
        self._pending, self.account, self.error = None, None, None
        self.auth_status = 'not_configured'
        self.messages, self.events = {}, {}

    def disconnect(self):
        with self._lock:
            self._token = self._refresh = ''
            self._expires = self._blocked_until = 0
            self._pending = self.account = self.error = None
            self.messages.clear()
            self.events.clear()
            self.auth_status = 'disconnected' if self.client_id and self._secret else 'not_configured'
        return self.status()

    def close(self):
        with self._lock:
            self.disconnect()
            self._secret, self.client_id = '', ''
            self.configuration_source = None
            self.auth_status = 'not_configured'

    def configure(self, client_id, client_secret, source='workspace'):
        if self._origin_error:
            raise HTTPException(400, self._origin_error)
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not re.fullmatch(r'[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', client_id):
            raise HTTPException(400, 'Google Cloud 웹 애플리케이션의 client ID를 입력하세요.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,256}', client_secret):
            raise HTTPException(400, 'Google Cloud의 client secret을 확인하세요.')
        with self._lock:
            if (client_id, client_secret) != (self.client_id, self._secret):
                self.disconnect()
                self.client_id, self._secret, self.auth_status = client_id, client_secret, 'disconnected'
            self.configuration_error = ''
            self.configuration_source = source
        return self.status()

    def status(self):
        with self._lock:
            auth = {'status': self.auth_status}
            if self._pending:
                remaining = max(0, int(self._pending['expires_at'] - self._clock()))
                if not remaining:
                    self._pending = None
                    self.auth_status = auth['status'] = 'expired'
                else:
                    auth['expires_in'] = remaining
            items = [item for item in self.store.all('connected_items') if item.get('provider') == 'google']
            return {'configured': bool(self.client_id and self._secret), 'connected': bool(self._token and self.account),
                    'client_id': self.client_id, 'client_secret_configured': bool(self._secret),
                    'redirect_uri': self.redirect_uri, 'account': self.account, 'auth': auth,
                    'scopes': SCOPES.copy(), 'read_only': True, 'error': self.error or self.configuration_error or None,
                    'configuration_error': self.configuration_error or None,
                    'configuration_source': self.configuration_source,
                    'imported': {'messages': sum(i['kind'] == 'email' for i in items),
                                 'events': sum(i['kind'] == 'event' for i in items), 'total': len(items)}}

    def start(self):
        with self._lock:
            if self.configuration_error:
                raise HTTPException(400, self.configuration_error)
            if not self.client_id or not self._secret:
                raise HTTPException(400, 'Google client ID와 client secret을 먼저 저장하세요.')
            self.disconnect()
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
            self._pending = {'state': state, 'verifier': verifier, 'expires_at': self._clock() + 600}
            self.auth_status = 'pending'
            query = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
                     'response_type': 'code', 'scope': ' '.join(SCOPES), 'state': state,
                     'code_challenge': challenge, 'code_challenge_method': 'S256',
                     'access_type': 'offline', 'prompt': 'consent select_account'}
            return {'authorization_url': AUTH_URL + '?' + urlencode(query), 'expires_in': 600}

    def _request(self, method, url, **kwargs):
        allowed = ((method == 'POST' and url == TOKEN_URL) or
                   (method == 'GET' and (url in (USER_URL, MAIL_URL, EVENTS_URL)
                    or re.fullmatch(re.escape(MAIL_URL) + r'/[a-fA-F0-9]{1,128}', url))))
        if not allowed:
            raise HTTPException(400, '허용되지 않은 Google 요청입니다.')
        if self._clock() < self._blocked_until:
            wait = max(1, int(self._blocked_until - self._clock() + .999))
            raise HTTPException(429, 'Google 요청 제한 대기 중입니다.', headers={'Retry-After': str(wait)})
        try:
            with self._factory() as client:
                with client.stream(method, url, **kwargs) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise HTTPException(502, 'Google 응답이 읽기 한도를 초과했습니다.')
                        chunks.append(chunk)
                    status, headers = response.status_code, response.headers
                    payload = b''.join(chunks)
        except httpx.HTTPError:
            raise HTTPException(502, 'Google 서비스에 연결하지 못했습니다. 잠시 후 다시 실행하세요.') from None
        if 300 <= status < 400:
            raise HTTPException(502, 'Google 서비스의 예상하지 못한 이동 응답입니다.')
        try:
            body = json.loads(payload)
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(502, 'Google 응답을 읽을 수 없습니다.') from None
        error = body.get('error') if isinstance(body.get('error'), dict) else {}
        reasons = {item.get('reason') for item in error.get('errors', []) if isinstance(item, dict)}
        if status == 429 or (status == 403 and reasons & {'rateLimitExceeded', 'userRateLimitExceeded'}):
            try:
                wait = int(headers.get('Retry-After', '30'))
            except ValueError:
                try:
                    wait = int((parsedate_to_datetime(headers['Retry-After']) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, KeyError):
                    wait = 30
            wait = min(3600, max(1, wait))
            self._blocked_until = self._clock() + wait
            raise HTTPException(429, 'Google 요청 한도에 도달했습니다. 안내된 시간 후 다시 실행하세요.', headers={'Retry-After': str(wait)})
        return status, body

    def _accept_token(self, body, initial=False):
        token = body.get('access_token')
        if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]{1,30000}', token) or str(body.get('token_type', '')).lower() != 'bearer':
            raise HTTPException(502, 'Google 인증 토큰 응답을 읽을 수 없습니다.')
        scopes = set(str(body.get('scope', '')).split())
        if initial and not set(SCOPES[2:]).issubset(scopes):
            raise HTTPException(403, 'Gmail과 캘린더 읽기 권한이 모두 필요합니다. 동의 항목을 확인한 뒤 다시 연결하세요.')
        try:
            ttl = min(86400, max(1, int(body.get('expires_in', 3600))))
        except (ValueError, TypeError):
            raise HTTPException(502, 'Google 인증 유효 시간을 읽을 수 없습니다.') from None
        self._token, self._expires = token, self._clock() + ttl
        refresh = body.get('refresh_token')
        if isinstance(refresh, str) and re.fullmatch(r'[\x21-\x7e]{1,30000}', refresh):
            self._refresh = refresh

    def callback(self, state, code='', error=''):
        with self._lock:
            self.status()
            if not self._pending or not re.fullmatch(r'[A-Za-z0-9_-]{43}', state or '') or not secrets.compare_digest(state, self._pending['state']):
                raise HTTPException(400, '연결을 시작한 같은 브라우저에서 다시 시도하세요. 인증 요청이 없거나 만료되었습니다.')
            pending, self._pending = self._pending, None
            if error:
                self.auth_status, self.error = 'denied', 'Google 연결이 승인되지 않았습니다. 동의 화면과 조직 정책을 확인하세요.'
                return False
            if not code or len(code) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in code):
                self.auth_status, self.error = 'error', 'Google 인증 코드를 확인하지 못했습니다. 다시 연결하세요.'
                return False
            try:
                status, body = self._request('POST', TOKEN_URL, data={'client_id': self.client_id,
                    'client_secret': self._secret, 'code': code, 'code_verifier': pending['verifier'],
                    'grant_type': 'authorization_code', 'redirect_uri': self.redirect_uri})
                if status >= 400:
                    raise HTTPException(400, 'Google 인증을 완료하지 못했습니다. 등록한 callback 주소·앱 설정·동의 항목을 확인하세요.')
                self._accept_token(body, initial=True)
                profile = self._api(USER_URL)
                account_id, email = clean(profile.get('sub'), 200), clean(profile.get('email'), 254)
                if not account_id or not email or profile.get('email_verified') is not True:
                    raise HTTPException(502, 'Google 계정 정보를 확인하지 못했습니다.')
                self.account = {'id': account_id, 'name': email, 'email': email}
                self.auth_status, self.error = 'connected', None
                return True
            except HTTPException as exc:
                self.disconnect()
                self.auth_status, self.error = 'error', exc.detail
                return False

    def _ensure_token(self, force=False):
        if not self._token:
            raise HTTPException(401, 'Google 계정을 먼저 연결하세요.')
        if not force and self._clock() + 30 < self._expires:
            return
        if not self._refresh:
            self.disconnect()
            self.auth_status = 'expired'
            raise HTTPException(401, 'Google 연결이 만료되었습니다. 다시 연결하세요.')
        status, body = self._request('POST', TOKEN_URL, data={'client_id': self.client_id,
            'client_secret': self._secret, 'refresh_token': self._refresh, 'grant_type': 'refresh_token'})
        if status >= 400:
            self.disconnect()
            self.auth_status = 'expired'
            raise HTTPException(401, 'Google 연결을 갱신하지 못했습니다. 다시 연결하세요.')
        try:
            self._accept_token(body)
        except HTTPException:
            self.disconnect()
            self.auth_status = 'expired'
            raise

    def _api(self, url, params=None):
        self._ensure_token()
        status, body = self._request('GET', url, params=params, headers={'Authorization': 'Bearer ' + self._token})
        if status == 401:
            self._ensure_token(force=True)
            status, body = self._request('GET', url, params=params, headers={'Authorization': 'Bearer ' + self._token})
        if status >= 400:
            message = {401: 'Google 연결이 만료되었습니다. 다시 연결하세요.',
                       403: 'Gmail·Calendar API 사용 설정, 읽기 권한 및 조직의 승인 정책을 확인하세요.',
                       404: 'Google에서 요청한 자료를 찾지 못했습니다.'}.get(status, 'Google 자료를 읽지 못했습니다. 잠시 후 다시 실행하세요.')
            if status == 401:
                self.disconnect()
                self.auth_status = 'expired'
            self.error = message
            raise HTTPException(status if status in (401, 403, 404) else 502, message)
        self.error = None
        return body

    def read_messages(self, limit=10, query=''):
        with self._lock:
            self.messages.clear()
            body = self._api(MAIL_URL, {'labelIds': 'INBOX', 'maxResults': limit, 'includeSpamTrash': 'false'})
            values = body.get('messages', [])
            if not isinstance(values, list):
                raise HTTPException(502, 'Gmail 목록을 읽을 수 없습니다.')
            items, warnings = [], []
            for value in values[:limit]:
                identity = value.get('id') if isinstance(value, dict) else None
                if not isinstance(identity, str) or not re.fullmatch(r'[a-fA-F0-9]{1,128}', identity):
                    raise HTTPException(502, 'Gmail 항목 식별자를 확인하지 못했습니다.')
                try:
                    raw = self._api(MAIL_URL + '/' + identity, {'format': 'full'})
                except HTTPException as exc:
                    if exc.status_code == 404:
                        warnings.append('조회 중 사라진 메일을 제외했습니다.')
                        continue
                    raise
                if raw.get('id') != identity:
                    raise HTTPException(502, 'Gmail 항목 응답이 일치하지 않습니다.')
                items.append(normalize_message(raw))
            self.messages = {item['id']: item for item in items}
            if query:
                items = [item for item in items if query.casefold() in (item['subject'] + '\n' + item['body'] + '\n' + item['from']).casefold()]
            return {'messages': items, 'count': len(items), 'truncated': bool(body.get('nextPageToken')),
                    'search_scope': '이번에 읽은 받은편지함 항목', 'warnings': warnings}

    def read_events(self, days=7, limit=25, start_date=None, end_date=None):
        if days not in (7, 14, 30, 90):
            raise HTTPException(400, '일정 조회 기간은 7일, 14일, 30일 또는 90일을 선택하세요.')
        if (start_date is None) != (end_date is None):
            raise HTTPException(400, '조회 시작일과 종료일을 함께 입력하세요.')
        korea = timezone(timedelta(hours=9))
        if start_date is not None:
            try:
                if not all(isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value)
                           for value in (start_date, end_date)):
                    raise ValueError()
                first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
            except ValueError:
                raise HTTPException(400, '조회 날짜는 YYYY-MM-DD 형식의 올바른 날짜로 입력하세요.') from None
            if last < first:
                raise HTTPException(400, '조회 종료일은 시작일보다 빠를 수 없습니다.')
            if (last - first).days + 1 > 366:
                raise HTTPException(400, '조회 기간은 시작일과 종료일을 포함해 366일 이내로 선택하세요.')
            try:
                # Date inputs describe Korean calendar days, including the end day.
                start = datetime(first.year, first.month, first.day, tzinfo=korea)
                following = last + timedelta(days=1)
                end = datetime(following.year, following.month, following.day, tzinfo=korea)
            except (ValueError, OverflowError):
                raise HTTPException(400, '조회 날짜가 지원 범위를 벗어났습니다.') from None
            range_start, range_end = first.isoformat(), last.isoformat()
        else:
            # Preserve the existing rolling window when no date range is supplied.
            start = datetime.now(timezone.utc)
            end = start + timedelta(days=days)
            range_start = start.astimezone(korea).date().isoformat()
            range_end = (end - timedelta(microseconds=1)).astimezone(korea).date().isoformat()
        with self._lock:
            self.events.clear()
            # Do not override timeZone: its value explains DATE boundaries.
            body = self._api(EVENTS_URL, {'timeMin': start.isoformat(), 'timeMax': end.isoformat(),
                'maxResults': limit, 'singleEvents': 'true', 'orderBy': 'startTime', 'showDeleted': 'false'})
            values = body.get('items', [])
            if not isinstance(values, list):
                raise HTTPException(502, 'Google 일정 목록을 읽을 수 없습니다.')
            items = [normalize_event(value, body.get('timeZone', '')) for value in values[:limit]
                     if isinstance(value, dict) and isinstance(value.get('id'), str)]
            self.events = {item['id']: item for item in items}
            return {'events': items, 'count': len(items), 'truncated': bool(body.get('nextPageToken')),
                    'from': start.isoformat(), 'to': end.isoformat(), 'calendar': 'primary',
                    'range_start': range_start, 'range_end': range_end}

    def import_selected(self, message_ids, event_ids, project):
        with self._lock:
            if any(identity not in self.messages for identity in message_ids) or any(identity not in self.events for identity in event_ids):
                raise HTTPException(400, '먼저 목록을 불러온 뒤 표시된 메일·일정을 선택하세요.')
            selected = [self.messages[i] for i in dict.fromkeys(message_ids)] + [self.events[i] for i in dict.fromkeys(event_ids)]
            return import_items(self.store, selected, project, (self.account or {}).get('id', ''))


class GoogleConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(min_length=1, max_length=200)
    # Validate the credential in configure(), whose HTTP errors never echo it.
    client_secret: str


class GoogleImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_ids: list[str] = Field(default_factory=list, max_length=25)
    event_ids: list[str] = Field(default_factory=list, max_length=50)
    project: str = Field(default='', max_length=100)


def install_google_workspace(app, ctx):
    ctx.google = GoogleWorkspaceConnector(ctx.store, callback_uri(ctx))
    # Explicit deployment app credentials are shared configuration, never a
    # user's access/refresh token. Every visitor still owns its own consent,
    # state, PKCE verifier, account, read cache and token lifetime.
    identity = os.environ.get('WORKMATE_GOOGLE_CLIENT_ID', '').strip()
    secret = os.environ.get('WORKMATE_GOOGLE_CLIENT_SECRET', '').strip()
    if identity and secret:
        try:
            ctx.google.configure(identity, secret, source='deployment')
        except HTTPException:
            ctx.google.configuration_error = ctx.google._origin_error or '서버의 Google 앱 설정을 확인해야 합니다. 관리자에게 알리거나 본인의 client ID와 secret을 입력하세요.'
    elif identity or secret:
        ctx.google.configuration_error = ctx.google._origin_error or '서버의 Google client ID와 secret을 함께 설정해야 합니다. 관리자에게 알리거나 본인의 앱 설정을 입력하세요.'

    @app.get('/api/google/status')
    def status():
        return ctx.google.status()

    @app.post('/api/google/config')
    def config(body: GoogleConfigRequest):
        return ctx.google.configure(body.client_id, body.client_secret)

    @app.post('/api/google/auth/start')
    def auth_start():
        return ctx.google.start()

    @app.get(CALLBACK_PATH)
    def auth_callback(request: Request):
        values = request.query_params
        # No code or state is reflected in the document or redirect. Deployment
        # also disables access logging, before this ASGI layer receives requests.
        request.scope['query_string'] = b''
        succeeded = ctx.google.callback(values.get('state', ''), values.get('code', ''), values.get('error', ''))
        return RedirectResponse('/?google_auth=' + ('connected' if succeeded else 'failed') + '#connections', status_code=303,
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                     'Content-Security-Policy': "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"})

    @app.delete('/api/google/connection')
    def disconnect():
        return ctx.google.disconnect()

    @app.get('/api/google/messages')
    def messages(limit: int = Query(default=10, ge=1, le=25), q: str = Query(default='', max_length=120)):
        return ctx.google.read_messages(limit, q)

    @app.get('/api/google/events')
    def events(days: int = Query(default=7), limit: int = Query(default=25, ge=1, le=50),
               start_date: str | None = Query(default=None, max_length=10),
               end_date: str | None = Query(default=None, max_length=10)):
        return ctx.google.read_events(days, limit, start_date, end_date)

    @app.post('/api/google/import')
    def selected_import(body: GoogleImportRequest):
        return ctx.google.import_selected(body.message_ids, body.event_ids, body.project)
