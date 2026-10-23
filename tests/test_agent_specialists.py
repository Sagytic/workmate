from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest

from backend.agent_hub import report_html
from backend.agent_specialists import (FocusOptions, draft_eml, focus_settings, plan_focus,
                                       schedule_ics, validate_drafts)
from backend.store import KST
from test_agent_hub import hub, run, wait


def event(store, identity, start, end, **metadata):
    return store.put('documents', {'id': identity, 'title': identity, 'project': 'ERP',
        'body': f'일정: {identity}\n시작: {start}\n종료: {end}', 'source_type': 'event',
        'source_metadata': {'start_at': start, 'end_at': end, **metadata}})


def future():
    return (datetime.now(KST).date() + timedelta(days=2)).isoformat()


def focus_run(client, store, ids, date=None):
    day = date or future()
    client.patch('/api/agent-hub/agents/hub_focus', json={'document_ids': ids})
    response = client.post('/api/agent-hub/agents/hub_focus/run', json={
        'request': '선택한 일정 사이에 업무를 배치해 주세요.',
        'focus': {'date': day, 'start': '09:00', 'end': '18:00', 'block_minutes': 50, 'break_minutes': 10}})
    assert response.status_code == 200, response.text
    result = wait(client, response.json()['id'])
    assert result['status'] == 'completed', result
    return result


def tasks(store, count):
    for i in range(count):
        store.put('tasks', {'id': f't{i}', 'title': f'정산 검토 {i}', 'status': 'todo',
                           'priority': 'high' if i == 0 else 'normal', 'project': 'ERP'})


def test_focus_merges_overlaps_and_exports_real_nonoverlapping_blocks(hub):
    client, ctx = hub
    day = future()
    event(ctx.store, 'meeting', day+'T10:00:00+09:00', day+'T11:00:00+09:00')
    event(ctx.store, 'overlap', day+'T10:30:00+09:00', day+'T12:00:00+09:00')
    event(ctx.store, 'afternoon', day+'T14:00:00+09:00', day+'T15:00:00+09:00')
    event(ctx.store, 'unselected', day+'T09:00:00+09:00', day+'T18:00:00+09:00')
    tasks(ctx.store, 9)
    output = focus_run(client, ctx.store, ['meeting', 'overlap', 'afternoon'])
    blocks = output['result']['time_blocks']
    assert len(blocks) == 6
    assert output['result']['focus_plan']['unscheduled_count'] == 3
    assert blocks[0]['title'] == '정산 검토 0'
    assert {b['start'][11:16] for b in blocks} == {'09:00','12:00','13:00','15:00','16:00','17:00'}
    assert all(b['evidence_verified'] for b in blocks)
    ics = client.get(f'/api/agent-hub/runs/{output["id"]}/schedule.ics')
    assert ics.status_code == 200 and ics.content.count(b'BEGIN:VEVENT') == 6
    assert b'DTSTART:' + day.replace('-','').encode() + b'T000000Z' in ics.content
    assert b'STATUS:TENTATIVE' in ics.content and b'ATTENDEE' not in ics.content
    assert all(len(line) <= 75 for line in ics.content.split(b'\r\n'))
    assert '집중 시간 제안' in client.get(output['artifacts'][0]['url']).text


def test_all_day_blocks_day_but_cancelled_and_free_events_do_not(hub):
    client, ctx = hub
    day = future()
    tomorrow = (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
    event(ctx.store, 'offsite', day, tomorrow, is_all_day=True)
    tasks(ctx.store, 2)
    blocked = focus_run(client, ctx.store, ['offsite'])
    assert blocked['result']['time_blocks'] == []
    assert client.get(f'/api/agent-hub/runs/{blocked["id"]}/schedule.ics').status_code == 409
    ctx.store.update('documents', 'offsite', source_metadata={'start_at': day, 'end_at': tomorrow, 'status': 'cancelled'})
    assert len(focus_run(client, ctx.store, ['offsite'])['result']['time_blocks']) == 2
    ctx.store.update('documents', 'offsite', source_metadata={'start_at': day, 'end_at': tomorrow, 'show_as': 'free'})
    assert len(focus_run(client, ctx.store, ['offsite'])['result']['time_blocks']) == 2


@pytest.mark.parametrize('flag', ['recurrence_unexpanded', 'time_uncertain'])
def test_uncertain_calendar_never_claims_free_slots(hub, flag):
    client, ctx = hub
    day = future()
    event(ctx.store, 'unknown', day+'T10:00:00+09:00', day+'T11:00:00+09:00', **{flag: True})
    output = focus_run(client, ctx.store, ['unknown'])
    assert output['result']['time_blocks'] == []
    assert output['result']['focus_plan']['warnings']
    assert output['verification']['status'] == 'needs_review'


def test_focus_validates_real_date_and_work_window():
    captured = datetime.now(KST).isoformat()
    for values in ({'date': '2026-02-30'}, {'date': future(), 'start':'18:00', 'end':'09:00'},
                   {'date': '2001-01-01'}, {'date': future(), 'start':'30:00'}):
        with pytest.raises(ValueError):
            focus_settings(values, captured)


def test_omitted_selected_calendar_blocks_focus_export(hub):
    client, ctx = hub
    day = future()
    ids = []
    for i in range(11):
        identity = 'long' + str(i)
        ctx.store.put('documents', {'id': identity, 'title': identity, 'body': '가' * 6000})
        ids.append(identity)
    event(ctx.store, 'busy_late', day+'T09:00:00+09:00', day+'T18:00:00+09:00')
    output = focus_run(client, ctx.store, ids + ['busy_late'])
    assert output['metrics']['calendar_sources_omitted'] == 1
    assert not output['result']['time_blocks']
    assert output['result']['focus_plan']['warnings']
    assert client.get(f'/api/agent-hub/runs/{output["id"]}/schedule.ics').status_code == 409


def test_busy_seconds_are_rounded_up_before_export(hub):
    client, ctx = hub
    day = future()
    event(ctx.store, 'seconds', day+'T08:00:00+09:00', day+'T09:00:35+09:00')
    output = focus_run(client, ctx.store, ['seconds'])
    block = output['result']['time_blocks'][0]
    assert block['start'][11:16] == '09:01' and block['end'][11:16] == '09:51'


def test_inbox_reads_selected_mail_and_produces_unsent_eml(hub):
    client, ctx = hub
    body = '보낸 사람: 서연 <seoyeon@example.com>\n제목: 승인 기준 확인\n액션: 승인 표 확인 | 담당: 김도윤 | 기한: 2026-10-28'
    ctx.store.put('documents', {'id':'mail','title':'승인 기준 확인','project':'ERP','body':body,
        'source_type':'email','source_metadata':{'sender':'seoyeon@example.com','subject':'승인 기준 확인'}})
    client.patch('/api/agent-hub/agents/hub_inbox', json={'document_ids':['mail']})
    output = run(client, kind='inbox', request='선택한 메일의 요청과 답장 초안을 정리해 주세요.')
    draft = output['result']['reply_drafts'][0]
    assert draft['recipient'] == 'seoyeon@example.com' and draft['evidence_verified']
    assert output['result']['actions'][0]['owner'] == '김도윤'
    eml = client.get(f'/api/agent-hub/runs/{output["id"]}/reply-drafts/0.eml')
    parsed = BytesParser(policy=policy.default).parsebytes(eml.content)
    assert parsed['To'] == 'seoyeon@example.com' and parsed['X-Unsent'] == '1'
    assert parsed['From'] is None and parsed['Bcc'] is None
    assert '안녕하세요' in parsed.get_content()
    assert client.get(f'/api/agent-hub/runs/{output["id"]}/reply-drafts/99.eml').status_code == 404
    assert '답장 초안' in client.get(output['artifacts'][0]['url']).text
    assert not ctx.store.all('tasks')  # analysis still has no implicit writes


def test_draft_validation_never_addresses_an_invented_mailbox_or_injects_headers():
    source = {'id':'mail','excerpt':'From: owner@example.com\nPlease check this.'}
    drafts, warnings = validate_drafts({'reply_drafts':[
        {'subject':'hello\r\nBcc: stolen@example.com','recipient':'outsider@example.com', 'body':'<script>hello</script>',
         'evidence_id':'mail','evidence_quote':'Please check this.'},
        {'subject':'reply','recipient':'owner@example.com','body':'body','evidence_id':'not-here','evidence_quote':'x'}
    ]}, [source])
    assert all(not d['recipient'] for d in drafts)
    parsed = BytesParser(policy=policy.default).parsebytes(draft_eml(drafts[0]))
    assert parsed['Bcc'] is None and parsed['To'] is None and len(warnings) == 1


def test_draft_address_must_match_whole_source_address():
    source = {'id':'mail', 'excerpt':'From: joann@example.com\nPlease check this.'}
    drafts, _ = validate_drafts({'reply_drafts': [{'subject':'Reply', 'recipient':'ann@example.com',
        'body':'Hello', 'evidence_id':'mail', 'evidence_quote':'Please check this.'}]}, [source])
    assert drafts[0]['evidence_verified'] and drafts[0]['recipient'] == ''


def test_meeting_preparation_and_followup_have_distinct_results(hub):
    client, ctx = hub
    day = future()
    event(ctx.store, 'agenda', day+'T10:00:00+09:00', day+'T11:00:00+09:00')
    client.patch('/api/agent-hub/agents/hub_meeting_prep', json={'document_ids':['agenda']})
    prep = run(client, kind='meeting_prep', request='이번 회의를 준비해 주세요.')
    assert '일정 1건' in prep['result']['summary']
    assert prep['result']['insights'][0]['evidence_verified']
    tasks(ctx.store, 2)
    followup = run(client, kind='followup', request='명시된 약속과 남은 회신을 정리해 주세요.')
    assert len(followup['result']['insights']) == 2
    assert all(i['evidence_verified'] for i in followup['result']['insights'])
