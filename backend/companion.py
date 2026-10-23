"""Personal planning, focus and contextual nudges backed by workspace records."""
from datetime import datetime, timedelta
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from .store import now, uid, KST, LOCK
from .desk_api import parsed_time


class Preferences(BaseModel):
    name: str = Field(default='모아', min_length=1, max_length=20)
    quiet: bool = False
    enabled: bool = True
    quiet_until: str | None = None


class PlanRequest(BaseModel):
    minutes: int = Field(default=120, ge=15, le=480)
    project: str = Field(default='', max_length=100)
    owner: str = Field(default='', max_length=80)
    start_at: str | None = None


class FocusRequest(BaseModel):
    task_id: str | None = None
    goal: str = Field(default='', max_length=200)
    minutes: int = Field(default=25, ge=1, le=180)


def tick(store):
    with LOCK:
        current = datetime.now(KST)
        for item in store.all('focus'):
            if item['status'] == 'active' and parsed_time(item['ends_at']) <= current:
                store.update('focus', item['id'], status='completed', completed_at=now())
                store.notify('focus_done_' + item['id'], '집중 시간이 끝났어요', item['goal'], item.get('task_id'))


def priority_tasks(store, project='', owner=''):
    current = datetime.now(KST)
    items = []
    for task in store.all('tasks'):
        if task.get('archived') or task['status'] == 'completed':
            continue
        if project and task.get('project') != project or owner and task.get('owner') != owner:
            continue
        due = parsed_time(task.get('due_at'))
        score, reasons = 0, []
        if due:
            if due < current:
                score += 100
                reasons.append('기한 지남')
            elif due.date() == current.date():
                score += 80
                reasons.append('오늘 마감')
            elif due < current + timedelta(days=3):
                score += 40
                reasons.append('3일 내 마감')
        if task.get('priority') == 'high':
            score += 30
            reasons.append('높은 우선순위')
        if task['status'] == 'waiting_input':
            score += 20
            reasons.append('보완 입력 필요')
        if task['status'] == 'failed':
            score += 25
            reasons.append('실행 결과 확인')
        items.append({'task_id': task['id'], 'title': task['title'], 'project': task.get('project', ''),
                      'owner': task.get('owner', ''), 'status': task['status'], 'due_at': task.get('due_at'),
                      'reason': ' · '.join(reasons) or '진행할 업무', 'score': score,
                      'duration': 15 if task['status'] in ('waiting_input', 'failed') else 30})
    return sorted(items, key=lambda x: (-x['score'], x['due_at'] or '9999', x['task_id']))


def install_companion(app, ctx):
    @app.get('/api/companion/plan/{day}/calendar.ics')
    def calendar(day: str):
        plan = ctx.require('plans', day)
        escape = lambda text: str(text).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\r', '').replace('\n', '\\n')
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Workmate//Personal Plan//KO', 'CALSCALE:GREGORIAN']
        for block in plan['blocks']:
            start, end = parsed_time(block['starts_at']), parsed_time(block['ends_at'])
            from datetime import timezone
            lines += ['BEGIN:VEVENT', 'UID:'+block['id']+'@workmate.local',
                      'DTSTAMP:'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                      'DTSTART:'+start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                      'DTEND:'+end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                      'SUMMARY:'+escape(block['title']), 'DESCRIPTION:'+escape(block['reason']), 'END:VEVENT']
        lines.append('END:VCALENDAR')
        return Response('\r\n'.join(lines)+'\r\n', media_type='text/calendar', headers={'Content-Disposition':'attachment; filename="workmate-plan.ics"'})

    @app.get('/api/companion')
    def companion():
        tick(ctx.store)
        prefs = ctx.store.get('preferences', 'companion') or {'name': '모아', 'quiet': False, 'enabled': True}
        items = priority_tasks(ctx.store)
        tasks = [t for t in ctx.store.all('tasks') if not t.get('archived')]
        today = datetime.now(KST).date()
        completed = [t for t in tasks if t['status'] == 'completed' and parsed_time(t.get('completed_at')) and parsed_time(t['completed_at']).date() == today]
        unread = [n for n in ctx.store.all('notifications') if not n.get('read') and not n.get('read_at')]
        runs = ctx.store.all('uat_runs')
        nudges = [{'id': n['id'], 'title': n['title'], 'body': n['message'], 'task_id': n.get('task_id'), 'type': 'notification'} for n in unread[:4]]
        if items:
            first = items[0]
            nudges.append({'id': 'priority_' + first['task_id'] + '_' + today.isoformat(), 'title': '먼저 확인해 볼까요?', 'body': first['title'] + ' · ' + first['reason'], 'task_id': first['task_id'], 'type': 'priority'})
        for run in runs[:2]:
            if run['status'] == 'completed':
                nudges.insert(0, {'id': 'uat_' + run['id'], 'title': 'UAT 결과가 준비됐어요', 'body': run['ticket']['title'], 'route': 'tickets', 'type': 'uat'})
        return {'preferences': prefs, 'priorities': items[:8], 'nudges': nudges,
                'focus': next((f for f in ctx.store.all('focus') if f['status'] in ('active', 'paused')), None),
                'focus_history': ctx.store.all('focus')[:10], 'plan': ctx.store.get('plans', today.isoformat()),
                'summary': {'open': len(items), 'completed_today': len(completed), 'unread': len(unread),
                            'waiting': sum(t['status'] == 'waiting_input' for t in tasks)}, 'server_time': now()}

    @app.patch('/api/companion/preferences')
    def preferences(body: Preferences):
        if not body.name.strip():
            raise ValueError('비서 이름을 입력하세요.')
        if body.quiet_until and not parsed_time(body.quiet_until):
            raise ValueError('알림 재개 시간을 확인하세요.')
        return ctx.store.put('preferences', {'id': 'companion', **body.model_dump(), 'updated_at': now()})

    @app.post('/api/companion/plan')
    def make_plan(body: PlanRequest):
        start = parsed_time(body.start_at) if body.start_at else datetime.now(KST).replace(second=0, microsecond=0)
        if not start:
            raise ValueError('계획 시작 시간을 확인하세요.')
        available, cursor, blocks = body.minutes, start, []
        candidates = priority_tasks(ctx.store, body.project, body.owner)
        for item in candidates:
            if item['status'] in ('queued', 'running'):
                continue
            duration = item['duration']
            if duration > available:
                continue
            blocks.append({**item, 'id': uid('block'), 'starts_at': cursor.isoformat(),
                           'ends_at': (cursor + timedelta(minutes=duration)).isoformat(), 'done': False})
            cursor += timedelta(minutes=duration)
            available -= duration
        return ctx.store.put('plans', {'id': start.date().isoformat(), 'created_at': now(), 'budget_minutes': body.minutes,
                                      'used_minutes': body.minutes - available, 'blocks': blocks,
                                      'project': body.project, 'owner': body.owner, 'method': '기한·우선순위·보완 상태를 기준으로 배치'})

    @app.patch('/api/companion/plan/{day}/{block_id}')
    def update_block(day: str, block_id: str, body: dict):
        if not isinstance(body.get('done'), bool):
            raise ValueError('완료 여부를 확인하세요.')
        with LOCK:
            plan = ctx.require('plans', day)
            block = next((b for b in plan['blocks'] if b['id'] == block_id), None)
            if block is None:
                raise HTTPException(404, '계획 항목이 없습니다.')
            block['done'] = body['done']
            return ctx.store.put('plans', plan)

    @app.post('/api/companion/focus')
    def start_focus(body: FocusRequest):
        with LOCK:
            tick(ctx.store)
            if any(f['status'] in ('active', 'paused') for f in ctx.store.all('focus')):
                raise ValueError('진행 중인 집중 시간을 먼저 마쳐 주세요.')
            task = ctx.require('tasks', body.task_id) if body.task_id else None
            goal = body.goal.strip() or (task['title'] if task else '')
            if not goal:
                raise ValueError('집중할 업무나 목표를 입력하세요.')
            return ctx.store.put('focus', {'id': uid('focus'), 'goal': goal, 'task_id': body.task_id,
                                         'minutes': body.minutes, 'started_at': now(), 'status': 'active',
                                         'ends_at': (datetime.now(KST) + timedelta(minutes=body.minutes)).isoformat()})

    @app.post('/api/companion/focus/{focus_id}/{action}')
    def focus_action(focus_id: str, action: str):
        with LOCK:
            tick(ctx.store)
            item = ctx.require('focus', focus_id)
            if action == 'pause' and item['status'] == 'active':
                return ctx.store.update('focus', focus_id, status='paused', remaining_seconds=max(0, (parsed_time(item['ends_at']) - datetime.now(KST)).total_seconds()))
            if action == 'resume' and item['status'] == 'paused':
                return ctx.store.update('focus', focus_id, status='active', ends_at=(datetime.now(KST) + timedelta(seconds=item['remaining_seconds'])).isoformat())
            if action == 'complete' and item['status'] in ('active', 'paused'):
                return ctx.store.update('focus', focus_id, status='completed', completed_at=now())
            raise ValueError('현재 상태에서 가능한 동작이 아닙니다.')
