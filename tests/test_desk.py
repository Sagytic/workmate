from datetime import datetime, timedelta
import json

import pytest

from backend import desk_agent as desk
from backend.desk_agent import run_desk
from backend.store import Store, KST


class MockPlanner:
    def __init__(self, response=None):
        self.configured = response is not None
        self.response = response
        self.calls = []

    def _complete(self, instructions, payload, max_tokens):
        self.calls.append((instructions, payload, max_tokens))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "desk.db")


def document(store, identifier="doc_a", body="승인은 담당자가 진행합니다.\n\n신청 기한은 금요일입니다.", **kwargs):
    return store.put("documents", {"id": identifier, "title": identifier, "body": body,
        "project": "Alpha", "category": "업무 지침", "synthetic": True, **kwargs})


def run(tmp_path, store, agent, inputs, planner=None):
    return run_desk(agent, inputs, tmp_path, store, planner or MockPlanner(), tmp_path / "result")


def test_rules_knowledge_quotes_real_paragraph_and_writes_three_safe_artifacts(tmp_path, store):
    doc = document(store, body='<script>alert("bad")</script>\n\n승인은 팀장이 진행합니다.')
    result = run(tmp_path, store, "knowledge", {"question": "승인은 누가 진행하나요?", "document_ids": [doc["id"]]})
    assert result["generation"] == "rules"
    assert result["synthetic"] is True
    assert result["metrics"]["documents"] == 1
    assert result["sources"][0]["quote"] in doc["body"]
    assert result["sources"][0]["id"] == "doc_a:p2"
    assert len(result["artifacts"]) == 3
    report = (tmp_path / "result" / "desk-report.html").read_text(encoding="utf-8")
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    saved = json.loads((tmp_path / "result" / "desk-result.json").read_text(encoding="utf-8"))
    assert saved["sources"] == result["sources"]
    assert store.all("tasks") == []


def test_live_citations_validate_exact_quotes_and_reject_invented_evidence(tmp_path, store):
    document(store)
    planner = MockPlanner({"answer": "담당자가 승인합니다. [doc_a:p1] [ghost:p1]",
        "citations": [{"id": "doc_a:p1", "quote": "승인은 담당자가 진행합니다."},
                      {"id": "doc_a:p2", "quote": "월요일입니다."},
                      {"id": "ghost:p1", "quote": "가짜 근거"}],
        "actions": [{"title": "승인 확인", "source_ids": ["doc_a:p1"], "priority": "high"},
                    {"title": "가짜 업무", "source_ids": ["ghost:p1"]}]})
    result = run(tmp_path, store, "knowledge", {"question": "승인 담당자는?"}, planner)
    assert result["generation"] == "llm"
    assert result["evidence_validation"]["accepted_model_citations"] == 1
    assert len(result["evidence_validation"]["rejected_model_citations"]) == 2
    assert len(result["sources"]) == 1
    source = result["sources"][0]
    assert source["id"] == "doc_a:p1"
    assert source["quote"] == "승인은 담당자가 진행합니다."
    assert source["document_id"] == "doc_a"
    assert len(source["document_sha256"]) == 64
    assert "[ghost:p1]" not in result["answer"]
    assert "[미확인 근거]" in result["answer"]
    assert [a["title"] for a in result["actions"]] == ["승인 확인"]
    assert store.all("tasks") == []
    assert 0 < planner.calls[0][2] <= 6000


def test_no_verified_model_sources_preserves_rules_result(tmp_path, store):
    document(store)
    result = run(tmp_path, store, "knowledge", {"question": "승인?"}, MockPlanner({
        "answer": "모든 승인이 완료되었습니다.", "citations": [], "sections": [{"title": "거짓", "body": "완료"}]}))
    assert "모든 승인이 완료" not in result["answer"]
    assert not result["sections"]
    assert any("채택하지 않았습니다" in warning for warning in result["warnings"])


def test_empty_knowledge_does_not_call_model_or_invent_answer(tmp_path, store):
    planner = MockPlanner({"answer": "should not run"})
    result = run(tmp_path, store, "knowledge", {"question": "근거는?", "project": "Missing"}, planner)
    assert planner.calls == []
    assert result["sources"] == []
    assert result["generation"] == "rules"
    assert "문서가 없습니다" in result["answer"]


def test_analyst_diff_is_exact_and_impact_label_is_separate(tmp_path, store):
    document(store, "before", "변경 없음\n\n납품일은 10월 25일입니다.")
    document(store, "after", "변경 없음\n\n납품일은 10월 28일입니다.\n\n<script>payload</script>")
    result = run(tmp_path, store, "analyst", {"before_id": "before", "after_id": "after"})
    assert result["metrics"]["change_blocks"] == 1
    assert result["changes"][0]["before_ids"] == ["before:p2"]
    assert result["changes"][0]["after_ids"] == ["after:p2", "after:p3"]
    assert any("미확정" in section["title"] for section in result["sections"])
    assert all(action["proposed"] for action in result["actions"])
    report = (tmp_path / "result" / "desk-report.html").read_text(encoding="utf-8")
    assert "<script>payload" not in report
    assert "&lt;script&gt;payload" in report


def test_analyst_unchanged_has_no_fake_change_or_actions(tmp_path, store):
    document(store, "old", "같은 본문")
    document(store, "new", "같은 본문")
    result = run(tmp_path, store, "analyst", {"before_id": "old", "after_id": "new"})
    assert result["metrics"]["change_blocks"] == 0
    assert result["actions"] == []


def test_reporter_filters_project_archive_and_does_not_infer_completion_time(tmp_path, store, monkeypatch):
    current = datetime(2026, 10, 22, 12, 0, tzinfo=KST)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current
    monkeypatch.setattr(desk, "datetime", FixedDatetime)
    def task(identifier, **extra):
        store.put("tasks", {"id": identifier, "title": identifier, "project": "Alpha", "status": "completed",
            "created_at": (current - timedelta(days=30)).isoformat(), "updated_at": current.isoformat(),
            "synthetic": True, **extra})
    task("unknown")
    task("recent", completed_at=(current - timedelta(days=1)).isoformat())
    task("old", completed_at=(current - timedelta(days=15)).isoformat())
    task("event", events=[{"at": (current - timedelta(days=2)).isoformat(), "status": "completed"}])
    task("archived", archived=True, completed_at=current.isoformat())
    task("other_project", project="Beta", completed_at=current.isoformat())
    task("overdue", status="waiting_input", due_at=(current - timedelta(days=1)).isoformat())
    task("failed", status="failed")
    result = run(tmp_path, store, "reporter", {"project": "Alpha", "period_days": 7})
    assert result["metrics"]["project_tasks"] == 6
    assert result["metrics"]["completed_in_period"] == 2
    assert result["metrics"]["completed_timing_unknown"] == 1
    assert result["metrics"]["overdue_open"] == 1
    assert result["metrics"]["waiting_input"] == 1
    assert result["metrics"]["failed"] == 1
    assert not any(source["task_id"] in ("archived", "other_project") for source in result["sources"])
    assert all(action["source_ids"] for action in result["actions"])
    assert len(store.all("tasks")) == 8


@pytest.mark.parametrize("report_type,heading", [("handover", "인수인계"), ("meeting_prep", "회의 준비")])
def test_reporter_alternative_formats_use_same_real_metrics(tmp_path, store, report_type, heading):
    result = run(tmp_path, store, "reporter", {"report_type": report_type})
    assert result["report_type"] == report_type
    assert result["metrics"]["project_tasks"] == 0
    assert any(heading in section["title"] for section in result["sections"])


@pytest.mark.parametrize("agent,inputs", [
    ("knowledge", {"question": ""}),
    ("knowledge", {"question": "q", "document_ids": ["missing"]}),
    ("knowledge", {"question": "q", "document_ids": "doc_a"}),
    ("analyst", {"before_id": "same", "after_id": "same"}),
    ("reporter", {"period_days": True}),
    ("reporter", {"period_days": 91}),
    ("reporter", {"report_type": "invent"}),
])
def test_invalid_inputs_are_rejected(tmp_path, store, agent, inputs):
    with pytest.raises(ValueError):
        run(tmp_path, store, agent, inputs)


def test_large_documents_bound_model_context_and_keep_paragraph_ids(tmp_path, store):
    document(store, "large", "\n\n".join(f"문단{i}: " + "내용 " * 400 for i in range(70)))
    planner = MockPlanner({"answer": "근거 부족", "citations": []})
    result = run(tmp_path, store, "knowledge", {"question": "문단69 확인"}, planner)
    supplied = planner.calls[0][1]["paragraphs"]
    assert len(supplied) <= 60
    assert sum(len(p["text"]) for p in supplied) <= 48000
    assert supplied[0]["id"] == "large:p70"
    assert any("길이 제한" in warning for warning in result["warnings"])


def test_live_failure_is_not_disguised_as_demo_success(tmp_path, store):
    document(store)
    with pytest.raises(ValueError, match="연결 실패"):
        run(tmp_path, store, "knowledge", {"question": "승인 담당자?"}, MockPlanner(ValueError("연결 실패")))
    assert not (tmp_path / "result" / "desk-result.json").exists()


def test_excessive_paragraph_count_is_bounded_before_diff(tmp_path, store):
    document(store, "large", "\n\n".join("문단" for _ in range(1001)))
    document(store, "after", "작은 문서")
    with pytest.raises(ValueError, match="1,000"):
        run(tmp_path, store, "analyst", {"before_id": "large", "after_id": "after"})


def test_completion_count_stays_truthful_when_source_list_is_capped(tmp_path, store):
    stamp = datetime.now(KST).isoformat()
    store.put('tasks', {'id': 'done', 'title': '완료한 작업', 'status': 'completed', 'completed_at': stamp, 'created_at': stamp})
    for number in range(60):
        store.put('tasks', {'id': f'todo_{number}', 'title': f'진행할 작업 {number}', 'status': 'todo', 'created_at': stamp})
    result = run(tmp_path, store, 'reporter', {'period_days': 7})
    assert result['metrics']['completed_in_period'] == 1
    assert not any('완료를 확인한 작업이 없습니다' in section['body'] for section in result['sections'])
    assert any('생략' in section['body'] for section in result['sections'])
