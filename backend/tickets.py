"""Ticket-linked browser acceptance evidence and portable delivery packages."""
import base64
import hashlib
import html
import json
import re
import zipfile
from copy import deepcopy
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .store import now, uid, KST, LOCK
from .builder import _browser_test

CRITERIA = {
    'create': ('업무 등록과 저장', ['TC-002', 'TC-003', 'TC-009']),
    'owner': ('담당자 필수 입력', ['TC-004']),
    'due': ('마감일 필수 입력', ['TC-005']),
    'past_due': ('과거 마감일 차단', ['TC-006']),
    'completion': ('완료 메모 필수 입력', ['TC-007', 'TC-008']),
    'search': ('검색과 상태 필터', ['TC-010']),
    'export': ('CSV 내보내기', ['TC-011']),
    'delete': ('삭제 확인과 취소', ['TC-012']),
}


class TicketRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=6000)
    app_id: str
    project: str = Field(default='', max_length=100)
    requester: str = Field(min_length=1, max_length=80)
    requester_email: str = Field(default='', max_length=254)
    assignee: str = Field(min_length=1, max_length=80)
    criteria: list[str] = Field(default_factory=lambda: ['create', 'search', 'export'], min_length=1, max_length=8)
    source_task_id: str | None = None


class RunRequest(BaseModel):
    executor: str = Field(min_length=1, max_length=80)


def seed_tickets(store):
    for index, (key, title, requester, assignee) in enumerate([
        ('requests', '고객 요청 접수·조회 인수 검수', '김서연', '박지훈'),
        ('settlements', '정산 차이 등록·내보내기 확인', '최유진', '이도현'),
        ('quotes', '견적·증빙 관리 인수 검수', '정민서', '한지우')], 1):
        ticket_id = 'workmate_ticket_' + key
        target = store.get('apps', 'demo_v3_app_' + key)
        if not target or store.get('tickets', ticket_id):
            continue
        store.put('tickets', {'id':ticket_id, 'code':f'WM-A{index:03}', 'title':title,
                  'requirement':f"{target['name']}에서 업무를 등록하고 다시 조회할 수 있어야 합니다. 검색과 상태 필터로 필요한 업무를 찾고 CSV로 내려받습니다.",
                  'app_id':target['id'], 'project':target.get('project',''), 'requester':requester,
                  'requester_email':f'{key}.review@example.com', 'assignee':assignee,
                  'criteria':['create','search','export'], 'source_task_id':None,
                  'created_at':now(), 'updated_at':now(), 'status':'ready'})


def validate_ticket(body, ctx):
    if any(not str(getattr(body, k)).strip() for k in ('title', 'requirement', 'requester', 'assignee')):
        raise ValueError('제목·요구사항·요청자·담당자를 입력하세요.')
    ctx.require('apps', body.app_id)
    if any(c not in CRITERIA for c in body.criteria):
        raise ValueError('지원하는 인수 기준을 선택하세요.')
    if body.requester_email and not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', body.requester_email):
        raise ValueError('요청자 이메일 형식을 확인하세요.')
    if body.source_task_id:
        task = ctx.require('tasks', body.source_task_id)
        if task.get('agent') != 'developer' or task.get('input', {}).get('app_id') != body.app_id:
            raise ValueError('선택한 개발 업무와 대상 앱이 일치해야 합니다.')


def trace_requirements(ticket, tests):
    indexed = {t['id']: t for t in tests}
    return [{'criterion': key, 'name': CRITERIA[key][0], 'case_ids': CRITERIA[key][1],
             'status': 'passed' if all(indexed.get(i, {}).get('status') == 'passed' for i in CRITERIA[key][1]) else 'review',
             'missing': [i for i in CRITERIA[key][1] if i not in indexed]} for key in ticket['criteria']]


def report_html(run, folder):
    esc = lambda value: html.escape(str(value or ''))
    ticket, result = run['ticket'], run['result']
    cases = result.get('tests', [])
    meta = [('티켓 번호', ticket['code']), ('프로젝트', ticket.get('project')), ('요청자', ticket['requester']),
            ('수신 주소', ticket.get('requester_email') or '미지정'), ('개발 담당자', ticket['assignee']),
            ('실행 담당자', run['executor']), ('실행 일시 (KST)', run['started_at']), ('완료 일시 (KST)', run['completed_at']),
            ('대상 업무앱', run['app_name']), ('앱 버전', run['app_version']), ('실행 환경', result.get('environment'))]
    rows = ''.join(f'<tr><td>{esc(c["name"])}</td><td>{esc(", ".join(c["case_ids"]))}</td><td>{"충족" if c["status"] == "passed" else "확인 필요"}</td></tr>' for c in run['traceability'])
    cards = []
    for case in cases:
        evidence = ''
        filename = case.get('screenshot')
        if filename and Path(filename).name == filename and (folder / filename).is_file():
            encoded = base64.b64encode((folder / filename).read_bytes()).decode()
            evidence = f'<img alt="{esc(case["name"])} 실행 화면" src="data:image/png;base64,{encoded}">'
        cards.append(f'<article><div class="case-head"><strong>{esc(case["id"])} · {esc(case["name"])}</strong><b class="{esc(case["status"])}">{"통과" if case["status"] == "passed" else "확인 필요"}</b></div><dl><dt>절차</dt><dd>{esc(case.get("steps") or case["name"])}</dd><dt>기대 결과</dt><dd>{esc(case.get("expected"))}</dd><dt>관찰 결과</dt><dd>{esc(case.get("actual"))}</dd><dt>실행 시각</dt><dd>{esc(case.get("started_at") or result.get("executed_at"))}</dd></dl>{evidence}</article>')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(ticket['code'])} UAT 결과서</title><style>
    :root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#11151c;color:#e7ebf1;font:15px/1.7 "Malgun Gothic",system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:48px 28px}}header{{border-bottom:2px solid #8badd4;padding-bottom:25px}}header p{{color:#a5afbe}}h1{{font-size:34px;margin:10px 0}}h2{{font-size:21px;margin-top:40px}}.kicker{{letter-spacing:2px;color:#8badd4;font-size:12px}}.status{{padding:12px 18px;background:#26374c;display:inline-block;border-radius:6px}}.metadata{{display:grid;grid-template-columns:repeat(2,1fr);gap:1px;background:#303947;border:1px solid #303947}}.metadata div{{padding:12px 18px;background:#191f29}}.metadata small{{display:block;color:#a5afbe}}.note{{background:#191f29;border-left:3px solid #8badd4;padding:16px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;border:1px solid #303947;text-align:left}}th{{background:#202733}}article{{margin:20px 0;border:1px solid #303947;border-radius:8px;overflow:hidden}}.case-head{{padding:16px;background:#202733;display:flex;justify-content:space-between;gap:15px}}dl{{display:grid;grid-template-columns:100px 1fr;gap:10px;padding:0 20px}}dt{{color:#a5afbe}}dd{{margin:0;overflow-wrap:anywhere}}img{{width:100%;display:block;border-top:1px solid #303947}}.passed{{color:#8cc4aa}}.failed,.blocked{{color:#ed9a8a}}code{{overflow-wrap:anywhere;color:#a5afbe}}a{{color:#8badd4}}.signoff{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;border:1px solid #303947;padding:20px}}@media(max-width:600px){{main{{padding:24px 16px}}.metadata,.signoff{{grid-template-columns:1fr}}h1{{font-size:26px}}dl{{grid-template-columns:1fr}}}}@media print{{:root{{color-scheme:light}}body{{background:white;color:#18212c;font-size:10pt}}main{{padding:0}}.metadata div,.case-head,.note,th{{background:#f2f4f7;color:#18212c}}.metadata small,dt,code{{color:#43536a}}a{{color:#234b78}}article{{break-inside:avoid}}img{{max-height:120mm;object-fit:contain}}.passed{{color:#1a6845}}.failed,.blocked{{color:#a43a2b}}@page{{size:A4;margin:12mm}}}}</style></head><body><main>
    <header><div class="kicker">WORKMATE / ACCEPTANCE REPORT</div><h1>{esc(ticket['title'])}</h1><p>{esc(ticket['code'])} · UAT 결과서</p><div class="status">자동 검증 {"통과" if run['passed'] else "확인 필요"} · 현업 인수 검토 대기</div></header>
    <h2>문서 정보</h2><div class="metadata">{''.join('<div><small>'+esc(k)+'</small>'+esc(v)+'</div>' for k,v in meta)}</div>
    <h2>요구사항과 검증 범위</h2><p>{esc(ticket['requirement'])}</p><p class="note">실제 업무앱 화면을 브라우저에서 실행했습니다. 업무 데이터는 격리된 메모리 API에서 등록·수정·조회하며 운영 저장소에는 반영하지 않습니다. 아래 인수 기준에 대응하는 실행 결과를 확인할 수 있습니다.</p>
    <table><thead><tr><th>인수 기준</th><th>연결 사례</th><th>결과</th></tr></thead><tbody>{rows}</tbody></table>
    <h2>실행 결과와 화면 증거</h2><p>{len(cases)}개 실행 항목 · 통과 {sum(c.get('status')=='passed' for c in cases)}개 · 확인 필요 {sum(c.get('status')!='passed' for c in cases)}개</p>{''.join(cards)}
    <h2>현업 인수 의견</h2><p>실행 결과 확인과 현업 인수 판정은 구분합니다. 요청자가 결과서를 검토한 후 판정을 남깁니다.</p><div class="signoff"><div>검토자<br>________________</div><div>검토일<br>________________</div><div>판정<br>□ 승인 □ 조건부 승인 □ 반려</div></div><p>의견: ____________________________________________________________________</p>
    <h2>추적 정보</h2><p>결과 ID: {esc(run['id'])}</p><code>소스 SHA-256: {esc(run['source_sha256'])}</code><p>문서 구성 참고: European Commission PM² Deliverables Acceptance Plan / Checklist. 한국어 실행 결과와 요구사항 추적·자동 캡처 구성을 Workmate에 맞게 작성했습니다.</p><p><a href="https://pm2.europa.eu/pm2-artefacts_en">PM² 공식 문서</a></p></main></body></html>'''


def execute_run(ctx, run_id):
    store, root = ctx.store, ctx.ROOT
    run = store.get('uat_runs', run_id)
    folder = root / 'data' / 'uat' / run_id
    try:
        store.update('uat_runs', run_id, status='running')
        result = _browser_test(folder / 'app.html', run['schema'], run['ticket']['app_id'], folder)
        trace = trace_requirements(run['ticket'], result.get('tests', []))
        passed = bool(result.get('passed')) and result.get('source_sha256') == run['source_sha256'] and all(c['status'] == 'passed' for c in trace)
        run.update(status='completed', completed_at=now(), result=result, traceability=trace, passed=passed)
        # Frozen run contains ticket and app snapshots, never mutable live metadata.
        report = report_html(run, folder)
        (folder / 'uat-report.html').write_text(report, encoding='utf-8')
        body = f"{run['ticket']['requester']}님,\n\n{run['ticket']['code']} {run['ticket']['title']}의 UAT 결과를 전달드립니다.\n실행 담당자: {run['executor']}\n실행 일시: {run['started_at']}\n자동 검증: {'통과' if passed else '확인 필요'}\n\n첨부 결과서의 요구사항 대응, 실행 결과, 화면 증거를 검토하시고 인수 의견을 회신해 주세요.\n현업 인수 판정은 아직 등록되지 않았습니다.\n"
        (folder / 'delivery.txt').write_text(body, encoding='utf-8')
        message = EmailMessage(policy=SMTP)
        message['Subject'] = f"[UAT 결과] {run['ticket']['code']} {run['ticket']['title']}"
        if run['ticket'].get('requester_email'):
            message['To'] = run['ticket']['requester_email']
        message['X-Unsent'] = '1'
        message.set_content(body)
        message.add_attachment(report.encode('utf-8'), maintype='text', subtype='html', filename=run['ticket']['code']+'-UAT.html')
        (folder / 'delivery.eml').write_bytes(message.as_bytes())
        public_result = {k: v for k, v in run.items() if k not in ('schema',)}
        (folder / 'result.json').write_text(json.dumps(public_result, ensure_ascii=False, indent=2), encoding='utf-8')
        names = ['uat-report.html', 'delivery.txt', 'delivery.eml', 'result.json'] + [p.name for p in folder.glob('evidence-*.png')]
        with zipfile.ZipFile(folder / 'uat-package.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                archive.write(folder / name, name)
        run['artifacts'] = [{'name': name, 'url': f'/api/uat/runs/{run_id}/files/{name}'} for name in names + ['uat-package.zip']]
        store.put('uat_runs', run)
        store.notify('uat_ready_' + run_id, 'UAT 결과서가 준비됐습니다', run['ticket']['title'])
    except Exception as exc:
        store.update('uat_runs', run_id, status='failed', completed_at=now(), error=str(exc)[:800], passed=False)


def install_tickets(app, ctx):
    @app.get('/api/tickets')
    def tickets():
        return {'tickets': ctx.store.all('tickets'), 'runs': ctx.store.all('uat_runs'),
                'criteria': [{'id': key, 'name': value[0]} for key, value in CRITERIA.items()]}

    @app.post('/api/tickets')
    def create_ticket(body: TicketRequest):
        validate_ticket(body, ctx)
        with LOCK:
            number = len(ctx.store.all('tickets')) + 1
            return ctx.store.put('tickets', {'id': uid('ticket'), 'code': f'WM-{number:04d}', **body.model_dump(),
                                             'created_at': now(), 'updated_at': now(), 'status': 'ready'})

    @app.patch('/api/tickets/{ticket_id}')
    def edit_ticket(ticket_id: str, body: TicketRequest):
        validate_ticket(body, ctx)
        ctx.require('tickets', ticket_id)
        return ctx.store.update('tickets', ticket_id, **body.model_dump(), updated_at=now())

    @app.post('/api/tickets/{ticket_id}/uat')
    def run_ticket(ticket_id: str, body: RunRequest):
        if not body.executor.strip():
            raise ValueError('실행 담당자를 입력하세요.')
        with LOCK:
            ticket = ctx.require('tickets', ticket_id)
            if any(r['ticket']['id'] == ticket_id and r['status'] in ('queued', 'running') for r in ctx.store.all('uat_runs')):
                raise ValueError('이 티켓의 UAT가 실행 중입니다.')
            target = ctx.require('apps', ticket['app_id'])
            source = Path(target['source_path']).resolve()
            if not source.is_relative_to((ctx.ROOT / 'data' / 'artifacts').resolve()) or not source.is_file():
                raise ValueError('대상 앱의 실행 파일을 확인하세요.')
            run_id = uid('uat')
            folder = ctx.ROOT / 'data' / 'uat' / run_id
            folder.mkdir(parents=True)
            source_bytes = source.read_bytes()
            (folder / 'app.html').write_bytes(source_bytes)
            run = {'id': run_id, 'status': 'queued', 'ticket': deepcopy(ticket), 'executor': body.executor.strip(),
                   'started_at': now(), 'app_name': target['name'], 'app_version': target.get('version', 1),
                   'source_sha256': hashlib.sha256(source_bytes).hexdigest(), 'schema': deepcopy(target['schema']),
                   'artifacts': [], 'approval': 'pending'}
            ctx.store.put('uat_runs', run)
            ctx.engine.pool.submit(execute_run, ctx, run_id)
            return run

    @app.get('/api/uat/runs/{run_id}')
    def get_run(run_id: str):
        return ctx.require('uat_runs', run_id)

    @app.get('/api/uat/runs/{run_id}/files/{filename}')
    def run_file(run_id: str, filename: str):
        run = ctx.require('uat_runs', run_id)
        if filename not in {a['name'] for a in run.get('artifacts', [])}:
            raise HTTPException(404, '결과물을 찾을 수 없습니다.')
        path = ctx.ROOT / 'data' / 'uat' / run_id / filename
        return FileResponse(path, **({'filename': filename} if path.suffix in ('.eml', '.zip') else {}))

    @app.post('/api/uat/runs/{run_id}/decision')
    def decision(run_id: str, body: dict):
        run = ctx.require('uat_runs', run_id)
        if run['status'] != 'completed':
            raise ValueError('완료된 결과를 먼저 확인하세요.')
        if body.get('decision') not in ('accepted', 'conditional', 'rejected') or not str(body.get('reviewer', '')).strip():
            raise ValueError('검토자와 인수 판정을 입력하세요.')
        if len(str(body.get('reviewer', ''))) > 80 or len(str(body.get('note', ''))) > 3000:
            raise ValueError('검토자 또는 의견이 너무 깁니다.')
        item = ctx.store.put('uat_decisions', {'id': uid('decision'), 'run_id': run_id, 'reviewer': body['reviewer'],
                            'decision': body['decision'], 'note': str(body.get('note', '')), 'created_at': now()})
        ctx.store.update('uat_runs', run_id, approval=body['decision'], latest_decision=item)
        return item
