"""Date-only action deadlines stay calendar dates throughout the workspace."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend import companion, desk_api, orchestrator
from backend.store import KST, Store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    clock = {'now': datetime(2026, 10, 23, 10, 0, tzinfo=KST)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock['now']
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    for module in (companion, desk_api, orchestrator):
        monkeypatch.setattr(module, 'datetime', Clock)
    return Store(tmp_path / 'dates.db'), clock


def add_task(store, identity, due_at, **changes):
    return store.put('tasks', {
        'id': identity, 'title': identity, 'project': 'ERP', 'owner': '김서연',
        'status': 'todo', 'priority': 'normal', 'archived': False,
        'due_at': due_at, 'updated_at': '2026-10-23T09:00:00+09:00', **changes,
    })


def test_project_risk_preserves_calendar_day_and_timestamp_instants(workspace):
    store, _ = workspace
    add_task(store, 'today', '2026-10-23')
    add_task(store, 'yesterday', '2026-10-22')
    add_task(store, 'past_timestamp', '2026-10-22T22:00:00Z')  # 07:00 KST
    add_task(store, 'future_timestamp', '2026-10-23T02:00:00Z')  # 11:00 KST
    add_task(store, 'done', '2026-10-22', status='completed')

    project = desk_api.project_summary(store)[0]
    assert project['overdue_count'] == 2
    assert project['risk_count'] == 2
    assert store.get('tasks', 'today')['due_at'] == '2026-10-23'


def test_priority_marks_today_all_day_and_keeps_timed_deadlines(workspace):
    store, _ = workspace
    add_task(store, 'today', '2026-10-23')
    add_task(store, 'yesterday', '2026-10-22')
    add_task(store, 'past_timestamp', '2026-10-23T00:30:00Z')  # 09:30 KST
    add_task(store, 'future_timestamp', '2026-10-23T04:00:00+02:00')  # 11:00 KST
    add_task(store, 'tomorrow', '2026-10-24')

    priorities = {item['task_id']: item for item in companion.priority_tasks(store)}
    assert priorities['today']['reason'] == '오늘 마감'
    assert priorities['today']['score'] == 80
    assert priorities['today']['due_at'] == '2026-10-23'
    assert priorities['yesterday']['reason'] == '기한 지남'
    assert priorities['past_timestamp']['reason'] == '기한 지남'
    assert priorities['future_timestamp']['reason'] == '오늘 마감'
    assert priorities['tomorrow']['reason'] == '3일 내 마감'


def test_date_notifications_do_not_invent_time_and_change_phase_next_day(workspace):
    store, clock = workspace
    add_task(store, 'calendar', '2026-10-23')
    add_task(store, 'tomorrow', '2026-10-24')
    engine = SimpleNamespace(store=store)

    assert orchestrator.Engine.check_notifications(engine) == 1
    note = store.all('notifications')[0]
    assert note['id'] == 'calendar:upcoming:2026-10-23'
    assert note['title'] == '오늘 마감'
    assert '기한 10/23 ·' in note['message'] and '00:00' not in note['message']
    assert orchestrator.Engine.check_notifications(engine) == 0

    clock['now'] += timedelta(days=1)
    assert orchestrator.Engine.check_notifications(engine) == 2
    overdue = store.get('notifications', 'calendar:overdue:2026-10-23')
    assert overdue['title'] == '기한이 되었습니다'
    assert '00:00' not in overdue['message']
    assert orchestrator.Engine.check_notifications(engine) == 0


def test_timestamp_notifications_keep_two_hour_window_and_kst_display(workspace):
    store, _ = workspace
    add_task(store, 'earlier', '2026-10-23T00:30:00Z')  # 09:30 KST
    add_task(store, 'within_window', '2026-10-23T02:00:00Z')  # 11:00 KST
    add_task(store, 'boundary', '2026-10-23T03:00:00Z')  # 12:00 KST
    add_task(store, 'later', '2026-10-23T03:01:00Z')
    engine = SimpleNamespace(store=store)

    assert orchestrator.Engine.check_notifications(engine) == 3
    notes = {note['task_id']: note for note in store.all('notifications')}
    assert notes['earlier']['title'] == '기한이 되었습니다'
    assert '09:30' in notes['earlier']['message']
    assert notes['within_window']['title'] == '마감 예정'
    assert '11:00' in notes['within_window']['message']
    assert notes['boundary']['title'] == '마감 예정'
    assert 'later' not in notes
    assert orchestrator.Engine.check_notifications(engine) == 0
