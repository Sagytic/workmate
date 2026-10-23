"""Project context, document library, examples and reviewed action handoff."""
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import File, Form, UploadFile
from fastapi import HTTPException
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field
from .store import LOCK, KST, now, uid

REFERENCES = [
    {'name':'Raycast Focus', 'url':'https://manual.raycast.com/focus', 'pattern':'빠른 대화와 업무별 집중 세션', 'implementation':'미니 비서의 빠른 요청·집중 타이머·알림 쉬기'},
    {'name':'Motion', 'url':'https://www.usemotion.com/features/ai-task-manager', 'pattern':'기한과 우선순위에 따른 시간 배치', 'implementation':'오늘 계획과 업무별 시간 블록·캘린더 파일'},
    {'name':'Atlassian 티켓 에이전트', 'url':'https://support.atlassian.com/jira-software-cloud/docs/collaborate-on-work-items-with-ai-agents/', 'pattern':'티켓 맥락에 작업과 결과 연결', 'implementation':'티켓 선택부터 UAT 화면 증거·결과서·전달 패키지까지'},
    {'name': 'Notion 3.8', 'url': 'https://www.notion.com/releases/2026-10-13',
     'pattern': '문서·업무 맥락을 에이전트들이 공유', 'implementation': '프로젝트 자료실, 근거 답변, 분석에서 후속 업무로 연결'},
    {'name': 'Atlassian Rovo · Jira Delivery', 'url': 'https://support.atlassian.com/rovo/docs/work-with-jira-delivery-agent/',
     'pattern': '실제 작업 상태로 위험과 진척을 보고', 'implementation': '프로젝트 위험 집계, 주간보고·인수인계·회의 준비'},
    {'name': 'Asana AI Studio', 'url': 'https://forum.asana.com/t/academy-training-getting-started-with-ai-studio/1123505',
     'pattern': '요청을 검토하고 누락 정보와 후속 업무를 정리', 'implementation': '변경 영향 분석, 제안 업무 선택 등록, 재사용 가능한 예제'},
]


def parsed_time(value):
    try:
        date = datetime.fromisoformat(value)
        return date.replace(tzinfo=KST) if date.tzinfo is None else date.astimezone(KST)
    except (TypeError, ValueError):
        return None


def date_only_deadline(value):
    """A calendar deadline must not acquire an invented midnight cutoff."""
    return isinstance(value, str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value))


def deadline_overdue(value, current):
    due = parsed_time(value)
    if due is None:
        return False
    current = current.replace(tzinfo=KST) if current.tzinfo is None else current.astimezone(KST)
    return due.date() < current.date() if date_only_deadline(value) else due < current


def project_summary(store):
    tasks = [t for t in store.all('tasks') if not t.get('archived')]
    docs = store.all('documents')
    definitions = {p['name']: p for p in store.all('projects')}
    names = set(definitions) | {t.get('project') for t in tasks} | {d.get('project') for d in docs}
    current = datetime.now(KST)
    result = []
    for name in sorted(n for n in names if n):
        group = [t for t in tasks if t.get('project') == name]
        open_tasks = [t for t in group if t['status'] != 'completed']
        overdue = [t for t in open_tasks if deadline_overdue(t.get('due_at'), current)]
        stale = [t for t in open_tasks if parsed_time(t.get('updated_at')) and parsed_time(t['updated_at']) < current - timedelta(days=7)]
        risk_ids = {t['id'] for t in overdue + stale + [t for t in open_tasks if t['status'] in ('waiting_input', 'failed')]}
        result.append({**definitions.get(name, {}), 'id': definitions.get(name, {}).get('id', name), 'name': name,
                       'color': definitions.get(name, {}).get('color', '#64748b'), 'task_count': len(group),
                       'open_count': len(open_tasks), 'completed_count': len(group) - len(open_tasks),
                       'overdue_count': len(overdue), 'stale_count': len(stale), 'risk_count': len(risk_ids),
                       'owners': sorted({t['owner'] for t in group if t.get('owner')}),
                       'document_count': sum(d.get('project') == name for d in docs)})
    return result


def demo_status(store):
    return {'available': bool(store.all('examples')), 'synthetic_task_count': sum(bool(t.get('synthetic')) for t in store.all('tasks')),
            'document_count': sum(bool(d.get('synthetic')) for d in store.all('documents')),
            'example_count': len(store.all('examples')), 'label': '업무 예제'}


class DocumentBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100000)
    category: str = Field(default='업무 자료', max_length=80)
    project: str = Field(default='', max_length=100)


class DocumentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=100000)
    category: str | None = Field(default=None, max_length=80)
    project: str | None = Field(default=None, max_length=100)


class ImportActions(BaseModel):
    action_ids: list[str] = Field(min_length=1, max_length=30)
    project: str | None = Field(default=None, max_length=100)


class TaskNote(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


def valid_document(values):
    for field in ('title', 'body'):
        if not isinstance(values.get(field), str) or not values[field].strip():
            raise ValueError('자료 제목과 본문을 입력하세요.')
    if '\x00' in values['body']:
        raise ValueError('본문은 일반 텍스트여야 합니다.')
    values['title'] = values['title'].strip()
    return values


def install_desk(app, api):
    @app.get('/api/documents')
    def documents(project: str = ''):
        return [d for d in api.store.all('documents') if not project or d.get('project') == project]

    @app.post('/api/documents')
    def create_document(body: DocumentBody):
        values = valid_document(body.model_dump())
        return api.store.put('documents', {'id': uid('doc'), **values, 'created_at': now(), 'updated_at': now(), 'synthetic': False})

    @app.post('/api/documents/upload')
    async def upload_document(file: UploadFile = File(...), project: str = Form(default='')):
        if Path(file.filename or '').suffix.lower() not in ('.txt', '.md'):
            raise ValueError('자료실에는 .txt 또는 .md 파일을 업로드하세요.')
        content = await file.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError('자료 파일은 1 MB 이하여야 합니다.')
        try:
            decoded = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                decoded = content.decode('cp949')
            except UnicodeDecodeError:
                raise ValueError('UTF-8 또는 한국어 CP949 텍스트 파일을 사용하세요.')
        if len(decoded) > 100000 or len(project) > 100:
            raise ValueError('자료 본문은 10만 자, 프로젝트명은 100자 이내여야 합니다.')
        filename = Path(file.filename.replace('\\', '/')).name
        values = valid_document({'title': filename[:200], 'body': decoded, 'project': project, 'category': '업로드 자료'})
        return api.store.put('documents', {'id': uid('doc'), **values, 'created_at': now(), 'updated_at': now(), 'synthetic': False})

    @app.get('/api/documents/{document_id}')
    def document(document_id: str):
        return api.require('documents', document_id)

    @app.patch('/api/documents/{document_id}')
    def edit_document(document_id: str, body: DocumentPatch):
        with LOCK:
            current = api.require('documents', document_id)
            changes = body.model_dump(exclude_unset=True)
            if any(v is None for v in changes.values()):
                raise ValueError('자료 설정값을 null로 변경할 수 없습니다.')
            valid_document({**current, **changes})
            return api.store.update('documents', document_id, **changes, updated_at=now())

    @app.delete('/api/documents/{document_id}')
    def delete_document(document_id: str):
        api.require('documents', document_id)
        api.store.delete('documents', document_id)
        return {'ok': True}

    @app.get('/api/documents/{document_id}/download')
    def download_document(document_id: str):
        doc = api.require('documents', document_id)
        return Response((doc['title'] + '\n\n' + doc['body']).encode('utf-8-sig'), media_type='text/plain; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{document_id}.txt"'})

    @app.get('/api/projects')
    def projects():
        return {'projects': project_summary(api.store)}

    @app.get('/api/search')
    def search(q: str = '', project: str = ''):
        if len(q) > 200:
            raise ValueError('검색어는 200자 이내로 입력하세요.')
        tokens = q.lower().split()
        if not tokens:
            return {'query': q, 'results': [], 'total': 0}
        items = []
        app_projects = {item['id']: item.get('project', '') for item in api.store.all('apps')}
        for collection, kind, title_field, body_field in [('tasks', 'task', 'title', 'summary'), ('documents', 'document', 'title', 'body'), ('apps', 'app', 'name', 'description'), ('records', 'record', 'title', ''), ('memories', 'memory', 'key', 'value')]:
            for obj in api.store.all(collection):
                object_project = app_projects.get(obj.get('app_id'), obj.get('project', '')) if kind == 'record' else obj.get('project', '')
                if obj.get('archived') or (project and object_project != project):
                    continue
                title = str(obj.get(title_field, ''))
                body = str(obj.get(body_field, '')) if body_field else ' '.join(str(v) for k, v in obj.items() if k not in ('id', 'app_id'))
                corpus = (title + ' ' + body + ' ' + str(obj.get('owner', '')) + ' ' + str(obj.get('project', ''))).lower()
                if not all(token in corpus for token in tokens):
                    continue
                position = max(0, body.lower().find(tokens[0]) - 60)
                item = {'id': obj['id'], 'type': kind, 'title': title, 'snippet': body[position:position + 220], 'project': object_project, 'synthetic': bool(obj.get('synthetic'))}
                if kind == 'task': item['task_id'] = obj['id']
                if kind == 'document': item['document_id'] = obj['id']
                if kind in ('app', 'record'):
                    item['app_id'] = obj['id'] if kind == 'app' else obj['app_id']
                    item['url'] = '/api/apps/' + item['app_id'] + '/view'
                items.append(item)
        items.sort(key=lambda item: (not all(t in item['title'].lower() for t in tokens), item['type'], item['title']))
        return {'query': q, 'results': items[:60], 'total': len(items)}

    @app.post('/api/tasks/{task_id}/actions/import')
    def import_actions(task_id: str, body: ImportActions):
        with LOCK:
            parent = api.require('tasks', task_id)
            if parent['status'] != 'completed':
                raise ValueError('분석이 완료된 뒤 후속 업무를 등록하세요.')
            actions = {str(a['id']): a for a in parent.get('result', {}).get('actions', []) if isinstance(a, dict) and a.get('id')}
            requested = list(dict.fromkeys(body.action_ids))
            if any(aid not in actions for aid in requested):
                raise ValueError('분석 결과에 포함된 후속 업무를 선택하세요.')
            tasks, created = [], 0
            for action_id in requested:
                action = actions[action_id]
                identity = 'task_action_' + hashlib.sha256((task_id + ':' + action_id).encode()).hexdigest()[:20]
                previous = api.store.get('tasks', identity)
                if previous:
                    tasks.append(previous)
                    continue
                title = str(action.get('title') or '').strip()[:200]
                if not title:
                    raise ValueError('후속 업무의 제목이 비어 있습니다.')
                try:
                    due = api.validate_due(action.get('due_at'))
                except (TypeError, ValueError):
                    due = None
                synthetic = bool(parent.get('synthetic') or parent.get('result', {}).get('synthetic'))
                task = {'id': identity, 'agent': 'assistant', 'title': title, 'status': 'todo', 'created_at': now(), 'updated_at': now(),
                        'due_at': due, 'priority': action.get('priority') if action.get('priority') in ('low', 'normal', 'high') else 'normal',
                        'project': body.project if body.project is not None else parent.get('project') or parent.get('input', {}).get('project', ''),
                        'owner': str(action.get('owner') or '')[:80], 'tags': ['후속 업무'], 'archived': False, 'mode': api.planner.mode,
                        'summary': '분석 제안을 확인하고 등록한 업무입니다.', 'input': {'manual': True, 'message': title},
                        'result': {}, 'events': [{'at': now(), 'message': '분석 결과에서 사용자가 선택해 등록했습니다.'}], 'artifacts': [],
                        'source_task_id': task_id, 'source_action_id': action_id, 'source_ids': action.get('source_ids', []),
                        'synthetic': synthetic, 'sourceLabel': '업무 예제' if synthetic else '분석 제안'}
                tasks.append(api.store.put('tasks', task))
                created += 1
            api.engine.check_notifications()
            return {'tasks': tasks, 'created': created, 'existing': len(tasks) - created}

    @app.post('/api/tasks/{task_id}/notes')
    def add_note(task_id: str, body: TaskNote):
        if not body.content.strip():
            raise ValueError('메모 내용을 입력하세요.')
        with LOCK:
            task = api.require('tasks', task_id)
            note = {'id': uid('note'), 'content': body.content.strip(), 'created_at': now()}
            notes = [*task.get('notes', []), note][-100:]
            api.store.update('tasks', task_id, notes=notes, updated_at=now())
            return note

    @app.delete('/api/tasks/{task_id}/notes/{note_id}')
    def delete_note(task_id: str, note_id: str):
        with LOCK:
            task = api.require('tasks', task_id)
            api.store.update('tasks', task_id, notes=[n for n in task.get('notes', []) if n['id'] != note_id], updated_at=now())
            return {'ok': True}

    @app.get('/api/examples')
    def examples():
        return api.store.all('examples')

    @app.get('/api/examples/files/{filename}')
    def example_file(filename: str):
        allowed = {'Excel_검증_기준본.xlsx', 'Excel_검증_수정본.xlsx', 'Excel_검증_예제_안내.html'}
        if filename not in allowed:
            raise HTTPException(404, '예제 파일을 찾을 수 없습니다.')
        path = api.ROOT / 'data' / 'examples' / 'excel_showcase' / filename
        if not path.is_file():
            raise HTTPException(404, '예제 파일이 아직 준비되지 않았습니다.')
        return FileResponse(path, filename=filename if path.suffix == '.xlsx' else None)

    @app.post('/api/examples/{example_id}/run')
    def run_example(example_id: str):
        example = api.require('examples', example_id)
        inputs = dict(example.get('input', {}))
        metadata = {'project': example.get('project', ''), 'synthetic': True, 'sourceLabel': '업무 예제', 'example_id': example_id}
        if example['agent'] == 'assistant':
            task = api.create_task(api.TaskRequest(agent='assistant', title=example['title'], input={**inputs, 'manual': True},
                                                   project=metadata['project'], due_at=(datetime.now(KST) + timedelta(hours=2)).isoformat()))
            return api.store.update('tasks', task['id'], **metadata)
        if example['agent'] == 'automation' and inputs.get('workflow_id'):
            workflow = api.require('workflows', inputs['workflow_id'])
            # The example is an explicit one-off run; saved schedules stay disabled.
            inputs.setdefault('baseline', workflow.get('baseline', 'baseline'))
            inputs.setdefault('current', workflow.get('current', 'changed'))
        return api.engine.submit(example['agent'], example['title'], inputs, metadata=metadata)

    @app.post('/api/demo/seed')
    def seed_examples():
        from .demo_data import seed_demo
        from .showcase_seed import seed_showcase
        with LOCK:
            result = seed_demo(api.ROOT, api.store)
            result['excel_showcase'] = seed_showcase(api.ROOT, api.store)
            return result
