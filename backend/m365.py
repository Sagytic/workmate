"""Read-only Microsoft Graph device authorization and selected source imports.

Client configuration, pending codes and tokens live in one workspace connector
instance only. Saved documents contain selected source snapshots, never tokens.
"""
import hashlib
import html
import json
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .store import LOCK, now

MAX_FILE = 2 * 1024 * 1024
MAX_BODY = 80000
SCOPES = ['User.Read', 'Mail.Read', 'Calendars.Read']
OAUTH_SCOPE = 'offline_access ' + ' '.join('https://graph.microsoft.com/' + s for s in SCOPES)
EXAMPLES = {
    'project-mail': {'filename': 'project-mail.eml', 'name': '프로젝트 협의 메일', 'kind': 'email', 'description': '요청·담당·협의 사항이 담긴 메일 파일'},
    'project-calendar': {'filename': 'project-calendar.ics', 'name': '프로젝트 일정', 'kind': 'event', 'description': '회의와 집중 일정이 담긴 캘린더 파일'},
}


def clean(value, limit=MAX_BODY):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', str(value or '')).strip()[:limit]


class PlainHTML(HTMLParser):
    HIDDEN = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'head', 'noscript', 'template'}
    BLOCKS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'section', 'hr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        if self.hidden or tag in self.HIDDEN:
            self.hidden.append(tag)
        elif tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                while self.hidden:
                    if self.hidden.pop() == tag:
                        break
        elif tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def plain_html(value):
    parser = PlainHTML()
    parser.feed(str(value or '')[:MAX_FILE])
    return clean(re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(parser.parts)))


def safe_link(value):
    value = clean(value, 2000)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        allowed = ('outlook.office.com', 'outlook.office365.com', 'outlook.live.com', 'outlook.cloud.microsoft')
        if parsed.scheme == 'https' and host in allowed and not parsed.username and not parsed.password and parsed.port in (None, 443):
            return value
    except ValueError:
        pass
    return ''


def _address(value):
    value = value.get('emailAddress', value) if isinstance(value, dict) else {}
    address, name = clean(value.get('address'), 254), clean(value.get('name'), 150)
    return f'{name} <{address}>' if name and name != address else address


def _iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.isoformat()
    except (ValueError, TypeError):
        return None


def _timezone(value):
    aliases = {'UTC': 'UTC', 'GMT': 'UTC', 'Etc/UTC': 'UTC', 'Korea Standard Time': 'Asia/Seoul',
               'Tokyo Standard Time': 'Asia/Tokyo', 'Eastern Standard Time': 'America/New_York',
               'Pacific Standard Time': 'America/Los_Angeles', 'GMT Standard Time': 'Europe/London',
               'W. Europe Standard Time': 'Europe/Berlin'}
    name = aliases.get(value, value)
    if name == 'UTC':
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _local_time(value, zone):
    """Return a verified offset, or preserve floating time with uncertainty."""
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        return parsed.isoformat(), False
    tz = _timezone(zone)
    if tz is None:
        return parsed.isoformat(), True
    first, second = parsed.replace(tzinfo=tz, fold=0), parsed.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != parsed:
        return parsed.isoformat(), True
    return first.isoformat(), False


def _graph_message(raw):
    body = raw.get('body') or {}
    text = plain_html(body.get('content')) if str(body.get('contentType')).lower() == 'html' else clean(body.get('content'))
    sender = _address(raw.get('from') or {})
    to = [_address(a) for a in (raw.get('toRecipients') or [])[:100]]
    return {'id': clean(raw.get('id'), 1024), 'kind': 'email', 'provider': 'm365', 'subject': clean(raw.get('subject'), 300) or '제목 없는 메일',
            'body': text, 'from': sender, 'sender': clean(((raw.get('from') or {}).get('emailAddress') or {}).get('address'), 254),
            'to': to, 'recipients': to, 'received_at': _iso(raw.get('receivedDateTime')), 'is_read': bool(raw.get('isRead')),
            'web_url': safe_link(raw.get('webLink')), 'web_link': safe_link(raw.get('webLink')),
            'warnings': ['본문 길이 제한으로 일부를 발췌했습니다.'] if len(str(body.get('content') or '')) > MAX_BODY else []}


def _graph_event(raw):
    body, start, end = raw.get('body') or {}, raw.get('start') or {}, raw.get('end') or {}
    all_day = bool(raw.get('isAllDay'))
    warnings = []
    try:
        # Graph returns dateTimeTimeZone, including for all-day events. Keep its
        # verified instants: cutting a UTC value to DATE can move a KST busy day.
        # Only an ICS VALUE=DATE is represented as a bare date in this connector.
        first, uncertain_start = _local_time(start['dateTime'], start.get('timeZone', ''))
        last, uncertain_end = _local_time(end['dateTime'], end.get('timeZone', ''))
        uncertain = uncertain_start or uncertain_end
        a, b = datetime.fromisoformat(first), datetime.fromisoformat(last)
        if (a.tzinfo is None) != (b.tzinfo is None) or b <= a:
            uncertain = True
    except (ValueError, TypeError, KeyError):
        first = last = None
        uncertain = True
    if uncertain:
        warnings.append('일정 시각 또는 시간대를 확정하지 못했습니다.')
    text = plain_html(body.get('content')) if str(body.get('contentType')).lower() == 'html' else clean(body.get('content'))
    if len(str(body.get('content') or '')) > MAX_BODY:
        warnings.append('본문 길이 제한으로 일부를 발췌했습니다.')
    return {'id': clean(raw.get('id'), 1024), 'kind': 'event', 'provider': 'm365',
            'subject': clean(raw.get('subject'), 300) or '제목 없는 일정', 'body': text,
            'start_at': first, 'end_at': last, 'time_zone': clean(start.get('timeZone'), 100),
            'is_all_day': all_day, 'end_exclusive': True, 'time_uncertain': uncertain,
            'location': clean((raw.get('location') or {}).get('displayName'), 500),
            'organizer': _address(raw.get('organizer') or {}),
            'attendees': [_address(a) for a in (raw.get('attendees') or [])[:100]],
            'web_url': safe_link(raw.get('webLink')), 'web_link': safe_link(raw.get('webLink')),
            'status': 'cancelled' if raw.get('isCancelled') else 'confirmed', 'show_as': clean(raw.get('showAs'), 40) or 'unknown',
            'recurrence': raw.get('recurrence'), 'recurrence_unexpanded': False,
            'instance_type': clean(raw.get('type'), 50), 'warnings': warnings}


def parse_eml(data):
    mail = BytesParser(policy=policy.default).parsebytes(data)
    if not any(mail.get(h) for h in ('Subject', 'From', 'Date', 'Message-ID')):
        raise HTTPException(400, '메일 헤더가 있는 EML 파일을 선택하세요.')
    candidates = []

    def visit(part):
        if part.get_content_disposition() == 'attachment' or part.get_filename() or part.get_content_type() == 'message/rfc822':
            return
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child)
        elif part.get_content_type() in ('text/plain', 'text/html'):
            candidates.append(part)
    visit(mail)
    chosen = next((p for p in candidates if p.get_content_type() == 'text/plain'), candidates[0] if candidates else None)
    body, warnings = '', []
    if chosen is not None:
        try:
            value = chosen.get_content()
        except (LookupError, UnicodeError):
            value = (chosen.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
            warnings.append('문자 인코딩 일부를 대체해 읽었습니다.')
        body = plain_html(value) if chosen.get_content_type() == 'text/html' else clean(value)
        if len(str(value)) > MAX_BODY:
            warnings.append('본문 길이 제한으로 일부를 발췌했습니다.')
    if not body:
        warnings.append('읽을 수 있는 일반 텍스트 본문이 없습니다.')
    sent, received = None, None
    if mail.get('Date'):
        try:
            sent = parsedate_to_datetime(str(mail['Date'])).isoformat()
        except (TypeError, ValueError, OverflowError):
            warnings.append('메일 날짜 형식을 확인하지 못했습니다.')
    if mail.get('Received'):
        try:
            received = parsedate_to_datetime(str(mail['Received']).rsplit(';', 1)[1].strip()).isoformat()
        except (TypeError, ValueError, OverflowError, IndexError):
            warnings.append('수신 헤더의 날짜를 확인하지 못했습니다.')
    addresses = getaddresses([str(mail.get('From') or '')])
    recipients = getaddresses([str(mail.get('To') or '')])
    pretty = lambda pairs: [clean(f'{name} <{address}>' if name else address, 400) for name, address in pairs][:100]
    message_id = clean(mail.get('Message-ID'), 1024) or hashlib.sha256(data).hexdigest()
    return [{'id': message_id, 'kind': 'email', 'provider': 'eml', 'subject': clean(mail.get('Subject'), 300) or '제목 없는 메일',
             'body': body, 'from': ', '.join(pretty(addresses)), 'sender': addresses[0][1] if addresses else '',
             'to': pretty(recipients), 'recipients': pretty(recipients), 'received_at': received, 'sent_at': sent,
             'date_source': 'Received' if received else 'Date' if sent else '',
             'is_read': False, 'web_url': '', 'web_link': '', 'warnings': warnings}]


def _split_ics(value, separator):
    parts, begin, quoted, escaped = [], 0, False, False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            parts.append(value[begin:index])
            begin = index + 1
    parts.append(value[begin:])
    return parts


def _ics_line(line):
    pieces = _split_ics(line, ':')
    if len(pieces) < 2:
        raise HTTPException(400, '일정 파일의 속성 형식을 확인하세요.')
    header, value = pieces[0], ':'.join(pieces[1:])
    pieces = _split_ics(header, ';')
    params = {}
    for piece in pieces[1:]:
        key, _, content = piece.partition('=')
        params[key.upper()] = content.strip('"')
    return pieces[0].upper(), params, value


def _ics_text(value):
    return clean(re.sub(r'\\([nN,;\\])', lambda m: '\n' if m[1].lower() == 'n' else m[1], value))


def _ics_date(params, value):
    if params.get('VALUE', '').upper() == 'DATE' or re.fullmatch(r'\d{8}', value):
        try:
            return datetime.strptime(value, '%Y%m%d').date().isoformat(), True, '', False
        except ValueError:
            raise HTTPException(400, '종일 일정의 날짜를 확인하세요.')
    if not re.fullmatch(r'\d{8}T\d{6}Z?', value):
        raise HTTPException(400, '일정의 날짜·시각 형식을 확인하세요.')
    try:
        parsed = datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S')
    except ValueError:
        raise HTTPException(400, '일정의 날짜·시각을 확인하세요.')
    if value.endswith('Z'):
        if params.get('TZID'):
            raise HTTPException(400, 'UTC 일정에 중복 시간대가 지정되어 있습니다.')
        return parsed.replace(tzinfo=timezone.utc).isoformat(), False, 'UTC', False
    zone = params.get('TZID', '')
    stamp, uncertain = _local_time(parsed.isoformat(), zone)
    return stamp, False, zone or 'floating', uncertain


def parse_ics(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise HTTPException(400, 'ICS 파일은 UTF-8 형식으로 저장해 주세요.')
    text = re.sub(r'\r?\n[ \t]', '', text)
    lines = [line.rstrip('\r') for line in text.split('\n') if line.strip()]
    if not lines or lines[0].upper() != 'BEGIN:VCALENDAR' or lines[-1].upper() != 'END:VCALENDAR':
        raise HTTPException(400, '올바른 VCALENDAR 형식의 ICS 파일을 선택하세요.')
    events, fields, depth, method = [], None, 0, ''
    for line in lines:
        key, params, value = _ics_line(line)
        if key == 'METHOD' and fields is None:
            method = value.upper()
        if key == 'BEGIN' and value.upper() == 'VEVENT':
            if fields is not None:
                raise HTTPException(400, '일정 항목의 중첩 형식을 확인하세요.')
            fields, depth = {}, 0
        elif fields is not None and key == 'BEGIN':
            depth += 1
        elif fields is not None and key == 'END' and depth:
            depth -= 1
        elif fields is not None and key == 'END' and value.upper() == 'VEVENT':
            events.append(fields)
            fields = None
            if len(events) > 100:
                raise HTTPException(400, 'ICS 파일은 일정 100개 이내로 나누어 업로드하세요.')
        elif fields is not None and depth == 0:
            fields.setdefault(key, []).append((params, value))
    if fields is not None or not events:
        raise HTTPException(400, '완결된 일정 항목이 있는 ICS 파일을 선택하세요.')
    output = []
    for fields in events:
        one = lambda key, default='': fields.get(key, [({}, default)])[0][1]
        if 'DTSTART' not in fields:
            raise HTTPException(400, '시작 날짜가 없는 일정은 가져올 수 없습니다.')
        start, all_day, zone, uncertain = _ics_date(*fields['DTSTART'][0])
        end, warnings = None, []
        if 'DTEND' in fields:
            end, end_all_day, _, end_uncertain = _ics_date(*fields['DTEND'][0])
            uncertain |= end_uncertain
            if all_day != end_all_day:
                raise HTTPException(400, '시작과 종료의 날짜 형식이 서로 다릅니다.')
            a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if (a.tzinfo is None) != (b.tzinfo is None):
                uncertain = True
            elif b <= a:
                raise HTTPException(400, '일정 종료는 시작 이후여야 합니다.')
        elif 'DURATION' in fields:
            duration = one('DURATION')
            match = re.fullmatch(r'P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', duration)
            if not match or not any(match.groups()):
                raise HTTPException(400, '일정 길이 형식을 확인하세요.')
            w, d, h, m, s = (int(v or 0) for v in match.groups())
            if max(w, d, h, m, s) > 100000 or (all_day and any((h, m, s))):
                raise HTTPException(400, '일정 길이가 날짜 형식과 맞지 않습니다.')
            try:
                base = datetime.fromisoformat(start)
                tz = _timezone(zone)
                if base.tzinfo is not None and tz is not None:
                    base = base.astimezone(tz)
                end_value = base + timedelta(weeks=w, days=d)
                if end_value.tzinfo is not None:
                    end_value = (end_value.astimezone(timezone.utc) + timedelta(hours=h, minutes=m, seconds=s)).astimezone(end_value.tzinfo)
                else:
                    end_value += timedelta(hours=h, minutes=m, seconds=s)
                end = end_value.date().isoformat() if all_day else end_value.isoformat()
            except OverflowError:
                raise HTTPException(400, '일정 길이가 너무 큽니다.')
        else:
            # RFC 5545 DATE DTSTART without DTEND means one calendar day.
            if all_day:
                try:
                    end = (datetime.fromisoformat(start) + timedelta(days=1)).date().isoformat()
                except OverflowError:
                    raise HTTPException(400, '종일 일정의 종료 날짜를 계산할 수 없습니다.') from None
                warnings.append('종료 날짜가 없어 ICS 종일 일정 규칙에 따라 다음 날을 종료 경계로 읽었습니다.')
            else:
                warnings.append('종료 시각이 없어 시간 확보 판단에 사용할 수 없습니다.')
        if uncertain:
            warnings.append('시간대가 없거나 확정할 수 없어 시각을 그대로 보존했습니다. 시간 확보 전에 시간대를 확인하세요.')
        recurrence = {key.lower(): [v for _, v in fields[key]] for key in ('RRULE', 'RDATE', 'EXDATE', 'EXRULE') if key in fields}
        if recurrence:
            warnings.append('반복 일정 규칙을 보존했습니다. 이 파일에서 반복 회차를 확장하지 않았습니다.')
        organizer = _ics_text(one('ORGANIZER')).removeprefix('mailto:').removeprefix('MAILTO:')
        attendees = [_ics_text(v).removeprefix('mailto:').removeprefix('MAILTO:') for _, v in fields.get('ATTENDEE', [])][:100]
        identity = one('UID') or hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        if one('RECURRENCE-ID'):
            identity += ':' + one('RECURRENCE-ID')
        output.append({'id': clean(identity, 1024), 'kind': 'event', 'provider': 'ics', 'subject': _ics_text(one('SUMMARY'))[:300] or '제목 없는 일정',
                       'body': _ics_text(one('DESCRIPTION')), 'start_at': start, 'end_at': end, 'time_zone': zone,
                       'is_all_day': all_day, 'end_exclusive': True, 'time_uncertain': uncertain,
                       'location': _ics_text(one('LOCATION'))[:500], 'organizer': organizer, 'attendees': attendees,
                       'web_url': '', 'web_link': '', 'status': 'cancelled' if method == 'CANCEL' or one('STATUS').upper() == 'CANCELLED' else 'confirmed',
                       'show_as': 'free' if one('TRANSP').upper() == 'TRANSPARENT' else 'busy',
                       'recurrence': recurrence or None, 'recurrence_unexpanded': bool(recurrence), 'warnings': warnings})
    return output


class ProviderError(HTTPException):
    pass


class M365Connector:
    def __init__(self, store, client_factory=None, clock=None):
        self.store = store
        self._factory = client_factory or (lambda: httpx.Client(timeout=25, verify=ssl.create_default_context(), follow_redirects=False))
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.client_id, self.tenant = '', 'organizations'
        self._token, self._refresh = '', ''
        self._token_expires, self._blocked_until = 0, 0
        self._pending, self.account, self.error = None, None, None
        self.auth_status = 'not_configured'
        self.messages, self.events = {}, {}

    def close(self):
        self.disconnect()

    def disconnect(self):
        with self._lock:
            self._token = self._refresh = ''
            self._token_expires = self._blocked_until = 0
            self._pending = self.account = self.error = None
            self.messages.clear()
            self.events.clear()
            self.auth_status = 'disconnected' if self.client_id else 'not_configured'
        return self.status()

    def configure(self, client_id, tenant):
        try:
            client_id = str(uuid.UUID(client_id.strip()))
        except (ValueError, AttributeError):
            raise HTTPException(400, '애플리케이션 ID는 Microsoft Entra의 UUID 형식 client ID를 입력하세요.')
        tenant = tenant.strip().lower()
        try:
            tenant = str(uuid.UUID(tenant))
            valid = True
        except ValueError:
            valid = tenant in ('common', 'organizations', 'consumers') or bool(re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', tenant))
        if not valid:
            raise HTTPException(400, '테넌트는 organizations·common·consumers, 테넌트 ID 또는 검증된 도메인을 입력하세요.')
        with self._lock:
            if (client_id, tenant) != (self.client_id, self.tenant):
                self.disconnect()
                self.client_id, self.tenant = client_id, tenant
                self.auth_status = 'disconnected'
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
                    auth.update(user_code=self._pending['user_code'], verification_uri=self._pending['verification_uri'],
                                expires_in=remaining, interval=self._pending['interval'],
                                next_poll_in=max(0, int(self._pending['next_poll'] - self._clock() + .999)))
            items = [item for item in self.store.all('connected_items') if item.get('provider') in ('m365', 'eml', 'ics')]
            return {'configured': bool(self.client_id), 'connected': bool(self._token and self.account),
                    'client_id': self.client_id, 'tenant': self.tenant, 'account': self.account,
                    'auth': auth, 'scopes': list(SCOPES), 'read_only': True, 'error': self.error,
                    'imported': {'messages': sum(i['kind'] == 'email' for i in items),
                                 'events': sum(i['kind'] == 'event' for i in items), 'total': len(items)}}

    def _request(self, method, url, **kwargs):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in ('login.microsoftonline.com', 'graph.microsoft.com') or parts.username or parts.password or parts.port not in (None, 443):
            raise HTTPException(400, '허용되지 않은 Microsoft 요청 주소입니다.')
        if self._clock() < self._blocked_until:
            wait = max(1, int(self._blocked_until - self._clock() + .999))
            raise ProviderError(429, 'Microsoft 요청 제한 대기 중입니다.', headers={'Retry-After': str(wait)})
        try:
            with self._factory() as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise ProviderError(502, 'Microsoft 서비스에 연결하지 못했습니다. 잠시 후 다시 실행하세요.') from None
        if response.status_code == 429:
            raw = response.headers.get('Retry-After', '30')
            try:
                wait = int(raw)
            except ValueError:
                try:
                    wait = int((parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError):
                    wait = 30
            wait = min(3600, max(1, wait))
            self._blocked_until = self._clock() + wait
            raise ProviderError(429, 'Microsoft 요청 한도에 도달했습니다. 안내된 시간 후 다시 실행하세요.', headers={'Retry-After': str(wait)})
        if 300 <= response.status_code < 400:
            raise ProviderError(502, 'Microsoft 서비스의 예상하지 못한 이동 응답입니다.')
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise ProviderError(502, 'Microsoft 응답을 읽을 수 없습니다.') from None
        return response.status_code, body

    def _auth_url(self, suffix):
        return f'https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/{suffix}'

    def start(self):
        with self._lock:
            if not self.client_id:
                raise HTTPException(400, '애플리케이션 ID와 테넌트를 먼저 저장하세요.')
            self.disconnect()
            status, body = self._request('POST', self._auth_url('devicecode'), data={'client_id': self.client_id, 'scope': OAUTH_SCOPE})
            if status >= 400:
                self.auth_status, self.error = 'error', 'Microsoft 인증을 시작하지 못했습니다. 앱 등록과 공개 클라이언트 흐름 허용을 확인하세요.'
                raise ProviderError(400, self.error)
            uri = str(body.get('verification_uri', ''))
            parsed = urlsplit(uri)
            if parsed.scheme != 'https' or parsed.hostname not in ('microsoft.com', 'www.microsoft.com', 'login.microsoftonline.com') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
                raise ProviderError(502, 'Microsoft 인증 확인 주소를 검증하지 못했습니다.')
            try:
                expires, interval = min(3600, max(1, int(body['expires_in']))), min(60, max(1, int(body.get('interval', 5))))
                user_code, device_code = clean(body['user_code'], 100), clean(body['device_code'], 10000)
                if not user_code or not device_code:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise ProviderError(502, 'Microsoft 인증 코드 응답을 읽을 수 없습니다.') from None
            self._pending = {'user_code': user_code, 'device_code': device_code, 'verification_uri': uri,
                             'expires_at': self._clock() + expires, 'interval': interval, 'next_poll': self._clock() + interval}
            self.auth_status = 'pending'
            return {'status': 'pending', 'user_code': user_code, 'verification_uri': uri, 'expires_in': expires, 'interval': interval}

    def _accept_token(self, body):
        token = body.get('access_token')
        if not isinstance(token, str) or not token or len(token) > 30000 or str(body.get('token_type', 'Bearer')).lower() != 'bearer':
            raise ProviderError(502, 'Microsoft 인증 토큰 응답을 읽을 수 없습니다.')
        try:
            ttl = min(86400, max(1, int(body.get('expires_in', 3600))))
        except (ValueError, TypeError):
            raise ProviderError(502, 'Microsoft 인증 유효 시간을 읽을 수 없습니다.') from None
        self._token, self._token_expires = token, self._clock() + ttl
        if isinstance(body.get('refresh_token'), str):
            self._refresh = body['refresh_token'][:30000]

    def poll(self):
        with self._lock:
            self.status()  # Expire a stale authorization before making any request.
            if not self._pending or self._clock() < self._pending['next_poll']:
                return self.status()
            pending = self._pending
            pending['next_poll'] = self._clock() + pending['interval']
            status, body = self._request('POST', self._auth_url('token'), data={'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                                         'client_id': self.client_id, 'device_code': pending['device_code']})
            if status >= 400:
                error = body.get('error')
                if error in ('authorization_pending', 'slow_down'):
                    if error == 'slow_down':
                        pending['interval'] = min(60, pending['interval'] + 5)
                        pending['next_poll'] = self._clock() + pending['interval']
                    return self.status()
                self._pending = None
                self.auth_status = 'expired' if error in ('expired_token', 'bad_verification_code') else 'denied' if error in ('authorization_declined', 'access_denied') else 'error'
                self.error = '인증 코드가 만료됐거나 승인이 거부되었습니다. 다시 연결하거나 조직의 인증 정책을 확인하세요.'
                return self.status()
            self._accept_token(body)
            self._pending = None
            try:
                profile = self._graph('/me', params={'$select': 'id,displayName,mail,userPrincipalName'})
            except HTTPException as exc:
                self.disconnect()
                self.auth_status, self.error = 'error', '인증 후 계정 정보를 확인하지 못했습니다. 읽기 권한과 조직 정책을 확인한 뒤 다시 연결하세요.'
                raise ProviderError(exc.status_code, self.error, headers=exc.headers) from None
            self.account = {'id': clean(profile.get('id'), 200), 'name': clean(profile.get('displayName'), 150),
                            'email': clean(profile.get('mail') or profile.get('userPrincipalName'), 254)}
            if not self.account['id']:
                self.disconnect()
                raise ProviderError(502, 'Microsoft 계정 정보를 확인하지 못했습니다.')
            self.auth_status, self.error = 'connected', None
            return self.status()

    def _ensure_token(self, force=False):
        if not self._token:
            raise HTTPException(401, 'Microsoft 계정을 먼저 연결하세요.')
        if not force and self._clock() + 30 < self._token_expires:
            return
        if not self._refresh:
            self.disconnect()
            self.auth_status = 'expired'
            raise HTTPException(401, 'Microsoft 연결이 만료되었습니다. 다시 연결하세요.')
        status, body = self._request('POST', self._auth_url('token'), data={'grant_type': 'refresh_token', 'client_id': self.client_id,
                                      'refresh_token': self._refresh, 'scope': OAUTH_SCOPE})
        if status >= 400:
            self.disconnect()
            self.auth_status = 'expired'
            raise ProviderError(401, 'Microsoft 연결을 갱신하지 못했습니다. 다시 연결하세요.')
        self._accept_token(body)

    def _graph(self, path, params=None):
        if path not in ('/me', '/me/mailFolders/inbox/messages', '/me/calendarView'):
            raise HTTPException(400, '지원하지 않는 Microsoft 읽기 요청입니다.')
        self._ensure_token()
        headers = {'Authorization': 'Bearer ' + self._token,
                   'Prefer': 'outlook.body-content-type="text", outlook.timezone="UTC"'}
        status, body = self._request('GET', 'https://graph.microsoft.com/v1.0' + path, params=params, headers=headers)
        if status == 401:
            self._ensure_token(force=True)
            headers['Authorization'] = 'Bearer ' + self._token
            status, body = self._request('GET', 'https://graph.microsoft.com/v1.0' + path, params=params, headers=headers)
        if status >= 400:
            messages = {401: 'Microsoft 연결이 만료되었습니다. 다시 연결하세요.',
                        403: '메일·일정 읽기 권한 또는 조직의 동의 정책을 확인하세요.',
                        404: '이 계정에서 사용할 수 있는 메일함 또는 일정을 찾지 못했습니다.'}
            message = messages.get(status, 'Microsoft 자료를 읽지 못했습니다. 잠시 후 다시 실행하세요.')
            if status == 401:
                self.disconnect()
                self.auth_status = 'expired'
            self.error = message
            raise ProviderError(status if status in (401, 403, 404) else 502, message)
        self.error = None
        return body

    def read_messages(self, limit=25, query=''):
        with self._lock:
            body = self._graph('/me/mailFolders/inbox/messages', params={'$top': limit, '$orderby': 'receivedDateTime desc',
                          '$select': 'id,subject,body,from,toRecipients,receivedDateTime,isRead,webLink'})
            values = body.get('value')
            if not isinstance(values, list):
                raise ProviderError(502, 'Microsoft 메일 목록을 읽을 수 없습니다.')
            items = [_graph_message(v) for v in values[:limit] if isinstance(v, dict) and isinstance(v.get('id'), str)]
            self.messages = {i['id']: i for i in items}
            if query:
                items = [i for i in items if query.casefold() in (i['subject'] + '\n' + i['body'] + '\n' + i['from']).casefold()]
            return {'messages': items, 'count': len(items), 'truncated': bool(body.get('@odata.nextLink')),
                    'search_scope': '이번에 읽은 받은편지함 항목'}

    def read_events(self, days=7, limit=50):
        with self._lock:
            start = datetime.now(timezone.utc)
            end = start + timedelta(days=days)
            body = self._graph('/me/calendarView', params={'startDateTime': start.isoformat(), 'endDateTime': end.isoformat(),
                          '$top': limit, '$orderby': 'start/dateTime',
                          '$select': 'id,subject,body,start,end,isAllDay,isCancelled,showAs,location,organizer,attendees,webLink,type,recurrence'})
            values = body.get('value')
            if not isinstance(values, list):
                raise ProviderError(502, 'Microsoft 일정 목록을 읽을 수 없습니다.')
            items = [_graph_event(v) for v in values[:limit] if isinstance(v, dict) and isinstance(v.get('id'), str)]
            self.events = {i['id']: i for i in items}
            return {'events': items, 'count': len(items), 'truncated': bool(body.get('@odata.nextLink')),
                    'from': start.isoformat(), 'to': end.isoformat()}

    def import_selected(self, message_ids, event_ids, project):
        with self._lock:
            if any(i not in self.messages for i in message_ids) or any(i not in self.events for i in event_ids):
                raise HTTPException(400, '먼저 목록을 불러온 뒤 표시된 메일·일정을 선택하세요.')
            selected = [self.messages[i] for i in dict.fromkeys(message_ids)] + [self.events[i] for i in dict.fromkeys(event_ids)]
            return import_items(self.store, selected, project, (self.account or {}).get('id', ''))


def import_items(store, records, project='', account_id=''):
    if not records:
        raise HTTPException(400, '가져올 메일 또는 일정을 선택하세요.')
    if len(project) > 100 or any(c in project for c in ('\x00', '\r', '\n')):
        raise HTTPException(400, '프로젝트 이름은 100자 이내 한 줄로 입력하세요.')
    documents, items, warnings, created = [], [], [], 0
    with LOCK:
        for record in records:
            # Source snapshots are versioned by content: unchanged imports reuse
            # records; a changed source creates a new snapshot without overwriting.
            metadata = {key: value for key, value in record.items() if key not in ('body', 'id')}
            digest = hashlib.sha256(json.dumps({'provider': record['provider'], 'account': account_id, 'id': record['id'],
                    'project': project.strip(), 'content': record}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
            identity, document_id = 'connected_' + digest, 'doc_connected_' + digest
            previous = store.get('connected_items', identity)
            doc = store.get('documents', document_id)
            if previous and doc:
                items.append(previous)
                documents.append(doc)
                continue
            if record['kind'] == 'email':
                headers = [f"제목: {record['subject']}", f"보낸 사람: {record.get('from', '')}",
                           f"받는 사람: {', '.join(record.get('to', []))}", f"수신 시각: {record.get('received_at') or '기록 없음'}"]
                if record.get('sent_at'):
                    headers.append(f"메일 작성 시각: {record['sent_at']}")
            else:
                headers = [f"일정: {record['subject']}", f"시작: {record.get('start_at') or '기록 없음'}", f"종료: {record.get('end_at') or '기록 없음'}",
                           f"시간대: {record.get('time_zone') or '날짜 기준'}", f"종일: {'예' if record.get('is_all_day') else '아니오'}",
                           f"장소: {record.get('location', '')}", f"주최자: {record.get('organizer', '')}",
                           f"참석자: {', '.join(record.get('attendees', []))}", f"상태: {record.get('status', '')}"]
            source_body = '\n'.join(headers) + '\n\n' + record.get('body', '')
            if record.get('warnings'):
                source_body += '\n\n읽기 안내:\n' + '\n'.join(record['warnings'])
            stamp = now()
            doc = {'id': document_id, 'title': record['subject'][:200], 'body': source_body[:100000], 'project': project.strip(),
                   'category': '메일' if record['kind'] == 'email' else '일정', 'created_at': stamp, 'updated_at': stamp,
                   'source_type': record['kind'], 'source_metadata': metadata, 'connected_item_id': identity}
            item = {'id': identity, 'kind': record['kind'], 'provider': record['provider'], 'source_id': record['id'],
                    'document_id': document_id, 'project': project.strip(), 'metadata': metadata, 'created_at': stamp}
            documents.append(store.put('documents', doc))
            items.append(store.put('connected_items', item))
            warnings.extend(record.get('warnings', []))
            created += 1
    return {'created': created, 'existing': len(items) - created, 'documents': documents, 'items': items,
            'warnings': list(dict.fromkeys(warnings))}


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(min_length=1, max_length=80)
    tenant: str = Field(default='organizations', min_length=1, max_length=253)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_ids: list[str] = Field(default_factory=list, max_length=50)
    event_ids: list[str] = Field(default_factory=list, max_length=50)
    project: str = Field(default='', max_length=100)


def install_m365(app, ctx):
    ctx.m365 = M365Connector(ctx.store)

    @app.get('/api/m365/status')
    def status():
        return ctx.m365.status()

    @app.post('/api/m365/config')
    def config(body: ConfigRequest):
        return ctx.m365.configure(body.client_id, body.tenant)

    @app.post('/api/m365/auth/start')
    def auth_start():
        return ctx.m365.start()

    @app.post('/api/m365/auth/poll')
    def auth_poll():
        return ctx.m365.poll()

    @app.delete('/api/m365/connection')
    def disconnect():
        return ctx.m365.disconnect()

    @app.get('/api/m365/messages')
    def messages(limit: int = Query(default=25, ge=1, le=50), q: str = Query(default='', max_length=120)):
        return ctx.m365.read_messages(limit, q)

    @app.get('/api/m365/events')
    def events(days: int = Query(default=7), limit: int = Query(default=50, ge=1, le=50)):
        if days not in (7, 14):
            raise HTTPException(400, '일정 조회 기간은 7일 또는 14일을 선택하세요.')
        return ctx.m365.read_events(days, limit)

    @app.post('/api/m365/import')
    def selected_import(body: ImportRequest):
        return ctx.m365.import_selected(body.message_ids, body.event_ids, body.project)

    @app.post('/api/m365/files')
    async def file_import(file: UploadFile = File(...), project: str = Form(default='')):
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in ('.eml', '.ics'):
            raise HTTPException(400, '메일은 .eml, 일정은 .ics 파일을 선택하세요.')
        data = await file.read(MAX_FILE + 1)
        if not data or len(data) > MAX_FILE:
            raise HTTPException(400, '비어 있지 않은 2MB 이내 파일을 선택하세요.')
        records = parse_eml(data) if suffix == '.eml' else parse_ics(data)
        return import_items(ctx.store, records, project)

    @app.get('/api/m365/examples')
    def examples():
        folder = Path(__file__).resolve().parents[1] / 'data' / 'examples' / 'outlook'
        return {'examples': [{'id': key, **value, 'download_url': '/api/m365/examples/' + key}
                             for key, value in EXAMPLES.items() if (folder / value['filename']).is_file()]}

    @app.get('/api/m365/examples/{example_id}')
    def example(example_id: str):
        item = EXAMPLES.get(example_id)
        if item is None:
            raise HTTPException(404, '파일을 찾을 수 없습니다.')
        path = Path(__file__).resolve().parents[1] / 'data' / 'examples' / 'outlook' / item['filename']
        if not path.is_file():
            raise HTTPException(404, '파일을 찾을 수 없습니다.')
        return FileResponse(path, filename=item['filename'], media_type='message/rfc822' if item['kind'] == 'email' else 'text/calendar')
