import time
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from backend import app as api
from backend.store import KST
from test_platform import client, make_app


def task(client, title='고객 검수 일정 확인', **extra):
    return client.post('/api/tasks', json={'agent':'assistant','title':title,'input':{'manual':True},**extra}).json()


def test_priority_plan_capacity_and_calendar(client):
    low = task(client, '기한 없는 업무', priority='low')
    urgent = task(client, '정산 기한 확인', priority='high', due_at=(datetime.now(KST)-timedelta(hours=1)).isoformat(), project='금융 정산')
    data = client.get('/api/companion').json()
    assert data['priorities'][0]['task_id'] == urgent['id']
    assert '기한 지남' in data['priorities'][0]['reason']
    plan = client.post('/api/companion/plan', json={'minutes':30, 'project':'금융 정산'}).json()
    assert len(plan['blocks']) == 1 and plan['used_minutes'] == 30
    assert plan['blocks'][0]['task_id'] == urgent['id']
    assert client.patch(f"/api/companion/plan/{plan['id']}/{plan['blocks'][0]['id']}",json={'done':True}).json()['blocks'][0]['done']
    # Completing a time block must never fabricate completion of the underlying task.
    assert client.get('/api/tasks/'+urgent['id']).json()['status'] == 'todo'
    calendar = client.get(f"/api/companion/plan/{plan['id']}/calendar.ics")
    assert calendar.status_code == 200 and 'BEGIN:VEVENT' in calendar.text and '정산 기한 확인' in calendar.text
    assert client.post('/api/companion/plan',json={'start_at':'not-a-date'}).status_code == 400


def test_focus_pause_resume_expiry_and_duplicate(client):
    t = task(client)
    f = client.post('/api/companion/focus',json={'task_id':t['id'],'minutes':1}).json()
    assert f['goal'] == t['title']
    assert client.post('/api/companion/focus',json={'goal':'중복'}).status_code == 400
    assert client.post(f"/api/companion/focus/{f['id']}/pause").json()['status'] == 'paused'
    assert client.post(f"/api/companion/focus/{f['id']}/pause").status_code == 400
    assert client.post(f"/api/companion/focus/{f['id']}/resume").json()['status'] == 'active'
    api.store.update('focus',f['id'],ends_at=(datetime.now(KST)-timedelta(seconds=1)).isoformat())
    data = client.get('/api/companion').json()
    assert data['focus'] is None and data['focus_history'][0]['status'] == 'completed'
    client.get('/api/companion')
    assert len([n for n in api.store.all('notifications') if n['id']=='focus_done_'+f['id']]) == 1


def test_preferences_persist_and_validate(client):
    assert client.patch('/api/companion/preferences',json={'name':'모아','quiet':True}).json()['quiet']
    assert client.get('/api/companion').json()['preferences']['quiet']
    assert client.patch('/api/companion/preferences',json={'name':' ','quiet_until':'bad'}).status_code == 400


def test_ticket_validation_and_missing_criteria(client):
    from backend.tickets import trace_requirements
    app_id = make_app(client)
    body={'title':'요청 관리 검수','requirement':'등록과 검색','app_id':app_id,'requester':'김현업','assignee':'박개발'}
    assert client.post('/api/tickets',json={**body,'requester_email':'x\r\nBcc: a@b.com'}).status_code == 400
    assert client.post('/api/tickets',json={**body,'criteria':['not-supported']}).status_code == 400
    ticket = client.post('/api/tickets',json=body).json()
    assert ticket['code'] == 'WM-0001'
    trace=trace_requirements({'criteria':['owner']},[{'id':'TC-003','status':'passed'}])
    assert trace[0]['status'] == 'review' and trace[0]['missing'] == ['TC-004']


def test_ticket_actual_browser_report_delivery_and_snapshot(client):
    app_id = make_app(client)
    body={'title':'요청 접수와 검색 확인','requirement':'요청을 등록하고 검색하며 CSV로 받는다.','app_id':app_id,
          'requester':'김현업','requester_email':'reviewer@example.com','assignee':'박개발','criteria':['create','search','export']}
    ticket=client.post('/api/tickets',json=body).json()
    run=client.post(f"/api/tickets/{ticket['id']}/uat",json={'executor':'이검수'}).json()
    client.patch(f"/api/tickets/{ticket['id']}",json={**body,'title':'나중에 변경한 제목'})
    for _ in range(240):
        run=client.get('/api/uat/runs/'+run['id']).json()
        if run['status'] not in ('queued','running'):break
        time.sleep(.25)
    assert run['status']=='completed',run
    assert run['passed'] is True,run.get('result')
    assert run['ticket']['title']==body['title']
    assert len([a for a in run['artifacts'] if a['name'].endswith('.png')])==len(run['result']['tests'])
    report=client.get(f"/api/uat/runs/{run['id']}/files/uat-report.html").text
    assert '이검수' in report and '김현업' in report and 'data:image/png;base64,' in report
    eml=client.get(f"/api/uat/runs/{run['id']}/files/delivery.eml").content
    message=BytesParser(policy=policy.default).parsebytes(eml)
    assert message['To']=='reviewer@example.com' and message['X-Unsent']=='1'
    assert len(list(message.iter_attachments()))==1
    assert run['approval']=='pending'
    assert client.get(f"/api/uat/runs/{run['id']}/files/browser-config.json").status_code==404
    response=client.post(f"/api/uat/runs/{run['id']}/decision",json={'reviewer':'김현업','decision':'conditional','note':'업무 기준 추가 확인'})
    assert response.status_code==200
    assert client.get('/api/uat/runs/'+run['id']).json()['approval']=='conditional'
