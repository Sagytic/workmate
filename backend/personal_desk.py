"""Personal source-linked briefs and durable, explicitly managed follow-ups.

This module has no model or connector client. Scheduled routines only inspect
the visitor's existing store; imported documents are data, never instructions.
"""
import hashlib
import re
from copy import deepcopy
from datetime import date as calendar_date, datetime, time, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .desk_api import deadline_overdue, parsed_time
from .store import KST, LOCK, now, uid


BRIEFS = 'desk_briefings'
FOLLOWUPS = 'desk_followups'
ROUTINES = 'desk_routines'
WRITING_ID = 'desk_writing'
DEFAULT_WRITING = {'enabled': False, 'tone': '정중하고 간결하게', 'language': '한국어',
                   'length': '짧게', 'signature': '', 'snippets': []}
MAX_BRIEFS, MAX_FOLLOWUPS, MAX_ROUTINES = 30, 100, 30
RUNTIME_NOTE = '예약 루틴은 서버와 이 워크스페이스가 실행 중일 때만 작동합니다. 메일·일정은 마지막으로 가져온 사본을 사용합니다.'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BriefRequest(StrictModel):
    project: str = Field(default='', max_length=100)
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    date: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')


class FollowupRequest(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=200)
    direction: Literal['owed', 'waiting']
    due_at: str | None = Field(default=None, max_length=50)
    note: str = Field(default='', max_length=2000)


class FollowupPatch(StrictModel):
    status: Literal['open', 'resolved'] | None = None
    snoozed_until: str | None = Field(default=None, max_length=50)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=2000)


class RoutineRequest(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    project: str = Field(default='', max_length=100)
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    time: str = Field(default='09:00', pattern=r'^\d{2}:\d{2}$')
    enabled: bool = True


class RoutinePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    document_ids: list[str] | None = Field(default=None, max_length=50)
    weekdays: list[int] | None = Field(default=None, min_length=1, max_length=7)
    time: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    enabled: bool | None = None


class Snippet(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1200)


class WritingPatch(StrictModel):
    enabled: bool | None = None
    tone: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=40)
    length: str | None = Field(default=None, max_length=40)
    signature: str | None = Field(default=None, max_length=1000)
    snippets: list[Snippet] | None = Field(default=None, max_length=12)


def _text(value, limit):
    if not isinstance(value, str):
        return ''
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()[:limit]


def _required(store, collection, identity):
    found = store.get(collection, identity)
    if found is None or found.get('archived'):
        raise HTTPException(404, '이 워크스페이스에서 항목을 찾을 수 없습니다.')
    return found


def _current(value=None):
    value = value or datetime.now(KST)
    return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)


def _due(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T\d{2}:\d{2})', value):
        raise HTTPException(400, '기한은 날짜 또는 ISO 날짜·시간으로 입력하세요.')
    if parsed_time(value) is None:
        raise HTTPException(400, '올바른 기한을 입력하세요.')
    # Retain an all-day date exactly; do not invent a midnight deadline.
    return value


def _scope(store, project, document_ids, strict=True):
    project = _text(project, 100)
    identities = list(dict.fromkeys(document_ids))
    if identities:
        docs = []
        for identity in identities:
            doc = store.get('documents', identity)
            if not doc or doc.get('archived'):
                if strict:
                    raise HTTPException(404, '선택한 자료가 이 워크스페이스에 없습니다.')
                continue
            if project and doc.get('project') != project:
                raise HTTPException(400, '선택한 자료와 프로젝트가 일치해야 합니다.')
            docs.append(doc)
    else:
        docs = [d for d in store.all('documents') if not d.get('archived') and
                (not project or d.get('project') == project) and d.get('source_type') in ('email', 'event')]
    return project, identities, docs


def _evidence(doc):
    return {'id': 'document:' + doc['id'], 'type': 'document', 'document_id': doc['id'],
            'title': _text(doc.get('title'), 200), 'quote': _text(doc.get('body'), 1800)}


def _writing(store):
    raw = store.get('preferences', WRITING_ID) or {}
    result = deepcopy(DEFAULT_WRITING)
    result['enabled'] = raw.get('enabled') is True
    for key, limit in (('tone', 80), ('language', 40), ('length', 40), ('signature', 1000)):
        if key in raw:
            result[key] = _text(raw[key], limit)
    result['snippets'] = [{'title': _text(s.get('title'), 80), 'text': _text(s.get('text'), 1200)}
                          for s in raw.get('snippets', [])[:12] if isinstance(s, dict) and
                          _text(s.get('title'), 80) and _text(s.get('text'), 1200)]
    return result


def writing_snapshot(store):
    """Return bounded, user-authored style guidance, not factual evidence."""
    values = _writing(store)
    return values if values['enabled'] else {}


def _followup_view(store, item, current):
    value = deepcopy(item)
    until = parsed_time(value.get('snoozed_until'))
    value['snoozed'] = value.get('status') == 'open' and bool(until and until > current)
    value['source_available'] = bool(store.get('documents', value.get('document_id')))
    return value


def build_brief(store, project='', document_ids=None, day=None, current=None, identity=None, routine_id=None):
    """Build a bounded readback with counts for the complete selected scope."""
    current = _current(current)
    try:
        target = calendar_date.fromisoformat(day) if day else current.date()
    except (ValueError, TypeError):
        raise HTTPException(400, '브리핑 날짜를 확인하세요.')
    if target > calendar_date.max - timedelta(days=8):
        raise HTTPException(400, '브리핑 날짜를 확인하세요.')
    project, identities, docs = _scope(store, project, document_ids or [])
    start = datetime.combine(target, time.min, KST)
    end = start + timedelta(days=1)
    comparison = current if target == current.date() else start
    warnings = ['가져온 자료의 사본과 현재 업무 기록을 집계했습니다. 전체 메일함·전체 일정의 실시간 상태를 의미하지 않습니다.']
    mail, events, tasks, evidence = [], [], [], []
    source_map = {d['id']: d for d in docs}
    uncertain_count = 0
    for doc in docs:
        meta = doc.get('source_metadata') if isinstance(doc.get('source_metadata'), dict) else {}
        shared = {'document_id': doc['id'], 'title': _text(doc.get('title'), 200),
                  'project': _text(doc.get('project'), 100), 'evidence_id': 'document:' + doc['id']}
        if doc.get('source_type') == 'email':
            # A saved EML does not establish read status in a live mailbox.
            read = meta.get('is_read') if meta.get('provider') in ('m365', 'google', 'gmail') else None
            mail.append({**shared, 'sender': _text(meta.get('sender') or meta.get('from'), 300),
                         'received_at': meta.get('received_at'), 'is_read': read if isinstance(read, bool) else None,
                         'excerpt': _text(doc.get('body'), 600), 'snapshot_at': doc.get('updated_at') or doc.get('created_at')})
        elif doc.get('source_type') == 'event':
            if meta.get('status') == 'cancelled' or meta.get('is_cancelled'):
                continue
            a, b = parsed_time(meta.get('start_at')), parsed_time(meta.get('end_at'))
            if meta.get('time_uncertain') or meta.get('recurrence_unexpanded') or not a or not b or b <= a:
                uncertain_count += 1
                continue
            if a < end and b > start:
                events.append({**shared, 'start_at': meta.get('start_at'), 'end_at': meta.get('end_at'),
                               'is_all_day': bool(meta.get('is_all_day')), 'time_uncertain': False,
                               'original_start_date': meta.get('original_start_date'),
                               'original_end_date': meta.get('original_end_date'),
                               'source_time_zone': _text(meta.get('time_zone'), 100),
                               'location': _text(meta.get('location'), 300)})
    if uncertain_count:
        warnings.append(f'시간·반복 범위를 확정할 수 없는 일정 사본 {uncertain_count}건은 날짜별 일정에서 제외했습니다. 원문을 확인하세요.')
    mail.sort(key=lambda row: (parsed_time(row.get('received_at')) or parsed_time(row.get('snapshot_at')) or start,
                               row['document_id']), reverse=True)
    events.sort(key=lambda row: (parsed_time(row['start_at']), row['document_id']))
    for task in store.all('tasks'):
        if task.get('archived') or task.get('status') in ('completed', 'cancelled', 'archived'):
            continue
        if project and task.get('project') != project:
            continue
        due = parsed_time(task.get('due_at'))
        if due is None or due.date() > target + timedelta(days=7):
            continue
        state = 'overdue' if deadline_overdue(task.get('due_at'), comparison) else 'today' if due.date() == target else 'upcoming'
        tasks.append({'task_id': task['id'], 'title': _text(task.get('title'), 200),
                      'project': _text(task.get('project'), 100), 'owner': _text(task.get('owner'), 100),
                      'priority': task.get('priority'), 'status': task.get('status'), 'due_at': task.get('due_at'),
                      'deadline_state': state, 'evidence_id': 'task:' + task['id']})
    tasks.sort(key=lambda row: (parsed_time(row['due_at']), row['task_id']))
    followups = [_followup_view(store, f, current) for f in store.all(FOLLOWUPS)
                 if f.get('status') == 'open' and not f.get('archived') and (not project or f.get('project') == project)
                 and (not identities or f.get('document_id') in identities)]
    counts = {'mail': len(mail), 'unread': sum(m['is_read'] is False for m in mail), 'events': len(events),
              'due_today': sum(t['deadline_state'] == 'today' for t in tasks),
              'overdue': sum(t['deadline_state'] == 'overdue' for t in tasks),
              'upcoming': sum(t['deadline_state'] == 'upcoming' for t in tasks),
              'followups': sum(not f['snoozed'] for f in followups)}
    for values, maximum, name in ((mail, 20, '메일 사본'), (events, 50, '일정 사본'), (tasks, 100, '기한 있는 업무')):
        if len(values) > maximum:
            warnings.append(f'{name} {len(values)}건 중 {maximum}건의 상세 내용을 표시합니다. 집계에는 전체 범위를 포함했습니다.')
    mail, events, tasks = mail[:20], events[:50], tasks[:100]
    for row in mail + events:
        evidence.append(_evidence(source_map[row['document_id']]))
    for task in tasks:
        evidence.append({'id': task['evidence_id'], 'type': 'task', 'task_id': task['task_id'],
                         'title': task['title'], 'quote': '\n'.join(f'{key}: {task[key]}' for key in ('title', 'status', 'due_at', 'owner'))})
    return {'id': identity or uid('brief'), 'date': target.isoformat(), 'captured_at': current.isoformat(timespec='seconds'),
            'scope': {'project': project, 'document_ids': identities, 'document_count': len(docs),
                      'mode': 'selected' if identities else 'imported', 'task_scope': 'project' if project else 'workspace'},
            'mail': mail, 'events': events, 'tasks': tasks, 'counts': counts, 'warnings': warnings,
            'evidence': evidence, 'routine_id': routine_id, 'method': 'stored_records', 'timezone': 'Asia/Seoul'}


def _save_brief(store, brief):
    store.put(BRIEFS, brief)
    # Store.all supplies newest insertion first. Stable sorting retains that
    # order for same-second captures, so a new result cannot prune itself.
    for old in sorted(store.all(BRIEFS), key=lambda b: b['captured_at'], reverse=True)[MAX_BRIEFS:]:
        store.delete(BRIEFS, old['id'])
    return brief


def _routine_values(store, values):
    values = {key: values[key] for key in ('name', 'project', 'document_ids', 'weekdays', 'time', 'enabled')}
    values['name'] = _text(values['name'], 100)
    if not values['name']:
        raise HTTPException(400, '루틴 이름을 입력하세요.')
    project, identities, _ = _scope(store, values['project'], values['document_ids'])
    values.update(project=project, document_ids=identities)
    if not values['weekdays'] or any(type(d) is not int or d < 0 or d > 6 for d in values['weekdays']):
        raise HTTPException(400, '요일은 월요일 0부터 일요일 6 사이에서 선택하세요.')
    values['weekdays'] = sorted(set(values['weekdays']))
    try:
        time.fromisoformat(values['time'])
    except (ValueError, TypeError):
        raise HTTPException(400, '루틴 시간을 00:00부터 23:59 사이로 입력하세요.')
    return values


def _next_slot(routine, current):
    hour, minute = map(int, routine['time'].split(':'))
    for offset in range(8):
        day = current.date() + timedelta(days=offset)
        slot = datetime.combine(day, time(hour, minute), KST)
        if day.weekday() in routine['weekdays'] and slot > current:
            return slot.isoformat(timespec='seconds')
    raise ValueError('Routine has no future weekday')


def _routine_view(value):
    return {**value, 'timezone': 'Asia/Seoul', 'runtime_note': RUNTIME_NOTE}


def tick(store, current=None):
    """One due run per slot, under the same lock as edits and manual runs.

    A resumed server skips older calendar days rather than replaying a backlog.
    No external provider access and no model generation happen in this path.
    """
    current = _current(current)
    with LOCK:
        for followup in store.all(FOLLOWUPS):
            until = parsed_time(followup.get('snoozed_until'))
            if followup.get('archived') or followup.get('status') != 'open' or not until or until > current:
                continue
            digest = hashlib.sha256((followup['id'] + '|' + followup['snoozed_until']).encode()).hexdigest()[:24]
            key = 'desk_followup_' + digest
            if store.notify(key, '다시 확인할 연락', _text(followup.get('title'), 200)):
                store.update('notifications', key, route='desk', followup_id=followup['id'])
        for routine in store.all(ROUTINES):
            if not routine.get('enabled') or routine.get('archived'):
                continue
            slot = parsed_time(routine.get('next_run_at'))
            if not slot:
                store.update(ROUTINES, routine['id'], next_run_at=_next_slot(routine, current))
                continue
            if slot.date() < current.date():
                store.update(ROUTINES, routine['id'], next_run_at=_next_slot(routine, current),
                             last_skip_reason='서버가 실행되지 않은 이전 날짜의 예약은 다시 실행하지 않습니다.')
                continue
            if slot > current:
                continue
            slot_key = slot.isoformat(timespec='seconds')
            next_slot = _next_slot(routine, current)
            if routine.get('last_scheduled_slot') == slot_key:
                store.update(ROUTINES, routine['id'], next_run_at=next_slot)
                continue
            digest = hashlib.sha256((routine['id'] + '|' + slot_key).encode()).hexdigest()[:24]
            notification_id = 'desk_routine_' + digest
            try:
                brief = build_brief(store, routine['project'], routine['document_ids'], current=current,
                                    identity='brief_routine_' + digest, routine_id=routine['id'])
                _save_brief(store, brief)
                counts = brief['counts']
                message = f"메일 사본 {counts['mail']} · 오늘 일정 {counts['events']} · 기한 지난 업무 {counts['overdue']} · 확인할 회신 {counts['followups']}"
                store.notify(notification_id, routine['name'] + ' 브리핑', message)
                store.update('notifications', notification_id, route='desk', briefing_id=brief['id'], routine_id=routine['id'])
                changes = {'last_briefing_id': brief['id'], 'last_error': None}
            except HTTPException as exc:
                # A deleted or re-scoped document must not widen a routine's scope.
                store.notify(notification_id, routine['name'] + ' 자료 확인', '선택한 자료 범위를 확인해 주세요. 예약 브리핑을 만들지 못했습니다.')
                store.update('notifications', notification_id, route='desk', routine_id=routine['id'])
                changes = {'last_error': str(exc.detail)}
            store.update(ROUTINES, routine['id'], last_run_at=current.isoformat(timespec='seconds'),
                         last_scheduled_slot=slot_key, next_run_at=next_slot, **changes)


def install_personal_desk(app, ctx):
    @app.get('/api/personal-desk/state')
    def state():
        with LOCK:
            current = _current()
            return {'preferences': _writing(ctx.store),
                    'followups': [_followup_view(ctx.store, f, current) for f in ctx.store.all(FOLLOWUPS) if not f.get('archived')],
                    'routines': [_routine_view(r) for r in ctx.store.all(ROUTINES) if not r.get('archived')],
                    'briefings': ctx.store.all(BRIEFS)[:MAX_BRIEFS], 'runtime_note': RUNTIME_NOTE, 'timezone': 'Asia/Seoul'}

    @app.post('/api/personal-desk/briefings')
    def briefing(body: BriefRequest):
        with LOCK:
            return _save_brief(ctx.store, build_brief(ctx.store, body.project, body.document_ids, body.date))

    @app.post('/api/personal-desk/followups')
    def create_followup(body: FollowupRequest):
        with LOCK:
            doc = _required(ctx.store, 'documents', body.document_id)
            title = _text(body.title, 200)
            if not title:
                raise HTTPException(400, '확인할 내용을 입력하세요.')
            due = _due(body.due_at)
            # Repeated clicks reuse the same open item; resolved records can be
            # reopened explicitly rather than silently replaced.
            existing = next((f for f in ctx.store.all(FOLLOWUPS) if not f.get('archived') and
                             f.get('status') == 'open' and f.get('document_id') == body.document_id and
                             f.get('direction') == body.direction and f.get('title') == title), None)
            if existing:
                return _followup_view(ctx.store, existing, _current())
            if len([f for f in ctx.store.all(FOLLOWUPS) if not f.get('archived')]) >= MAX_FOLLOWUPS:
                raise HTTPException(409, '회신 관리에는 100건까지 저장할 수 있습니다. 오래된 항목을 보관하세요.')
            stamp = now()
            row = {'id': uid('followup'), 'title': title, 'document_id': doc['id'],
                   'source_title': _text(doc.get('title'), 200), 'source_quote': _text(doc.get('body'), 1800),
                   'evidence_id': 'document:' + doc['id'], 'project': _text(doc.get('project'), 100),
                   'direction': body.direction, 'due_at': due, 'note': _text(body.note, 2000),
                   'status': 'open', 'snoozed_until': None, 'origin': 'user',
                   'created_at': stamp, 'updated_at': stamp, 'resolved_at': None}
            ctx.store.put(FOLLOWUPS, row)
            return _followup_view(ctx.store, row, _current())

    @app.patch('/api/personal-desk/followups/{identity}')
    def update_followup(identity: str, body: FollowupPatch):
        with LOCK:
            row = _required(ctx.store, FOLLOWUPS, identity)
            values = body.model_dump(exclude_unset=True)
            if any(values.get(key) is None for key in ('status', 'title', 'note') if key in values):
                raise HTTPException(400, '변경할 값을 입력하세요.')
            if 'title' in values:
                values['title'] = _text(values['title'], 200)
                if not values['title']:
                    raise HTTPException(400, '확인할 내용을 입력하세요.')
            if 'note' in values:
                values['note'] = _text(values['note'], 2000)
            if 'due_at' in values:
                values['due_at'] = _due(values['due_at'])
            current = _current()
            if values.get('snoozed_until') is not None:
                until = parsed_time(values['snoozed_until'])
                if not until or until <= current or until > current + timedelta(days=366):
                    raise HTTPException(400, '다시 알림은 현재 이후 1년 이내로 지정하세요.')
                values['snoozed_until'] = until.isoformat(timespec='seconds')
            if values.get('status') == 'resolved':
                values.update(resolved_at=now(), snoozed_until=None)
            elif values.get('status') == 'open':
                values['resolved_at'] = None
            saved = ctx.store.update(FOLLOWUPS, row['id'], **values, updated_at=now())
            return _followup_view(ctx.store, saved, current)

    @app.delete('/api/personal-desk/followups/{identity}')
    def remove_followup(identity: str):
        with LOCK:
            _required(ctx.store, FOLLOWUPS, identity)
            ctx.store.update(FOLLOWUPS, identity, archived=True, snoozed_until=None, updated_at=now())
            return {'archived': True}

    @app.get('/api/personal-desk/writing')
    def writing():
        return _writing(ctx.store)

    @app.patch('/api/personal-desk/writing')
    def save_writing(body: WritingPatch):
        with LOCK:
            changes = body.model_dump(exclude_unset=True)
            if any(value is None for value in changes.values()):
                raise HTTPException(400, '작성 스타일 값은 빈 문자열 또는 올바른 값으로 입력하세요.')
            values = {**_writing(ctx.store), **changes}
            for snippet in values['snippets']:
                if not _text(snippet['title'], 80) or not _text(snippet['text'], 1200):
                    raise HTTPException(400, '상용구 이름과 내용을 입력하세요.')
            ctx.store.put('preferences', {'id': WRITING_ID, **values, 'updated_at': now()})
            return _writing(ctx.store)

    @app.post('/api/personal-desk/routines')
    def create_routine(body: RoutineRequest):
        with LOCK:
            if len([r for r in ctx.store.all(ROUTINES) if not r.get('archived')]) >= MAX_ROUTINES:
                raise HTTPException(409, '루틴은 30개까지 저장할 수 있습니다.')
            values = _routine_values(ctx.store, body.model_dump())
            current = _current()
            row = {'id': uid('routine'), **values, 'created_at': now(), 'updated_at': now(),
                   'next_run_at': _next_slot(values, current) if values['enabled'] else None,
                   'last_run_at': None, 'last_briefing_id': None, 'last_error': None}
            ctx.store.put(ROUTINES, row)
            return _routine_view(row)

    @app.patch('/api/personal-desk/routines/{identity}')
    def update_routine(identity: str, body: RoutinePatch):
        with LOCK:
            row = _required(ctx.store, ROUTINES, identity)
            changes = body.model_dump(exclude_unset=True)
            if any(value is None for value in changes.values()):
                raise HTTPException(400, '루틴 설정값을 확인하세요.')
            values = _routine_values(ctx.store, {**row, **changes})
            reset = any(values[key] != row.get(key) for key in ('time', 'weekdays', 'enabled'))
            if reset:
                values['next_run_at'] = _next_slot(values, _current()) if values['enabled'] else None
            saved = ctx.store.update(ROUTINES, identity, **values, updated_at=now())
            return _routine_view(saved)

    @app.post('/api/personal-desk/routines/{identity}/run')
    def run_routine(identity: str):
        with LOCK:
            row = _required(ctx.store, ROUTINES, identity)
            brief = _save_brief(ctx.store, build_brief(ctx.store, row['project'], row['document_ids'], routine_id=identity))
            ctx.store.update(ROUTINES, identity, last_run_at=now(), last_briefing_id=brief['id'], last_error=None)
            return brief

    @app.delete('/api/personal-desk/routines/{identity}')
    def remove_routine(identity: str):
        with LOCK:
            _required(ctx.store, ROUTINES, identity)
            ctx.store.update(ROUTINES, identity, archived=True, enabled=False, next_run_at=None, updated_at=now())
            return {'archived': True}
