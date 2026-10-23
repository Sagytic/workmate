import io
import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from backend import app as api
from backend.llm import Planner, demo_plan
from backend.orchestrator import Engine
from backend.store import Store, KST


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ("WORKMATE_LLM_API_KEY", "WORKMATE_LLM_BASE_URL", "WORKMATE_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    shutil.copytree(Path(__file__).parents[1] / "data" / "fixtures", tmp_path / "data" / "fixtures")
    store = Store(tmp_path / "data" / "test.db")
    planner = Planner()
    engine = Engine(tmp_path, store, planner)
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "planner", planner)
    monkeypatch.setattr(api, "engine", engine)
    with TestClient(api.app) as c:
        yield c


def wait(c, task_id):
    for _ in range(200):
        task = c.get("/api/tasks/" + task_id).json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(.05)
    raise AssertionError("Task did not finish")


def make_app(c):
    response = c.post("/api/apps", json={"prompt": "고객 요청 관리 앱"})
    assert response.status_code == 200, response.text
    task = wait(c, response.json()["id"])
    assert task["status"] == "completed", task
    return task["result"]["app_id"]


def test_memory_persists_and_can_be_deleted(client):
    answer = client.post("/api/chat", json={"message": "기억해줘 보고 형식: 한 페이지 요약"}).json()
    mid = answer["memory"]["id"]
    # New requests do not depend on in-memory chat history.
    brief = client.post("/api/chat", json={"message": "오늘 업무 브리핑"}).json()
    assert "한 페이지 요약" in brief["reply"]
    assert client.post("/api/memories", json={"key": "보고 형식", "value": "두 페이지"}).json()["id"] == mid
    assert client.delete("/api/memories/" + mid).status_code == 200
    assert all(m["id"] != mid for m in client.get("/api/state").json()["memories"])


def test_excel_evidence_download_and_original_preserved(client):
    before = api.engine.resolve_input("changed").read_bytes()
    task = client.post("/api/tasks", json={"agent": "excel", "input": {"baseline": "baseline", "current": "changed"}}).json()
    task = wait(client, task["id"])
    assert task["status"] == "completed", task
    assert len(task["result"]["findings"]) >= 6
    assert task["result"]["recalculated"] is False
    assert api.engine.resolve_input("changed").read_bytes() == before
    assert task["artifacts"]
    for artifact in task["artifacts"]:
        assert client.get(artifact["url"]).status_code == 200
    assert client.get(f"/api/artifacts/{task['id']}/missing.json").status_code == 404
    assert client.post(f"/api/tasks/{task['id']}/resume", json={"action": "repair"}).status_code == 200
    repaired = wait(client, task["id"])
    assert len(repaired["artifacts"]) > len(task["artifacts"])
    assert any(a["name"].endswith("repaired.xlsx") for a in repaired["artifacts"])
    for artifact in repaired["artifacts"]:
        assert client.get(artifact["url"]).status_code == 200


def test_app_real_crud_and_server_validation(client):
    app_id = make_app(client)
    assert "<html" in client.get(f"/api/apps/{app_id}/view").text
    assert client.post(f"/api/apps/{app_id}/records", json={"status": "대기"}).status_code == 400
    created = client.post(f"/api/apps/{app_id}/records", json={"title": "샘플 요청", "owner": "김담당", "status": "대기"}).json()
    assert created["id"]
    updated = client.patch(f"/api/apps/{app_id}/records/{created['id']}", json={"status": "진행"}).json()
    assert updated["status"] == "진행"
    assert len(client.get(f"/api/apps/{app_id}/records").json()) == 1
    assert client.post(f"/api/apps/{app_id}/records", json={"title": "x", "status": "잘못된 상태"}).status_code == 400


def test_workflow_blocks_repairs_and_resumes_once(client):
    app_id = make_app(client)
    wf = client.post("/api/workflows", json={"name": "검수 등록", "app_id": app_id}).json()
    t = client.post(f"/api/workflows/{wf['id']}/run", json={"current": "changed"}).json()
    t = wait(client, t["id"])
    assert t["status"] == "waiting_input"
    assert client.get(f"/api/apps/{app_id}/records").json() == []
    repaired = client.post(f"/api/tasks/{t['id']}/resume", json={"action": "repair"})
    assert repaired.status_code == 200, repaired.text
    t = wait(client, t["id"])
    assert t["status"] == "waiting_input", "Data problems cannot be bypassed by formula repair"
    continued = client.post(f"/api/tasks/{t['id']}/resume", json={"action": "continue", "input": {"current": "clean"}})
    assert continued.status_code == 200
    t = wait(client, t["id"])
    assert t["status"] == "completed", t
    assert len(client.get(f"/api/apps/{app_id}/records").json()) == 1
    assert client.post(f"/api/tasks/{t['id']}/resume", json={"action": "continue"}).status_code == 400
    assert len(client.get(f"/api/apps/{app_id}/records").json()) == 1


def test_real_upload_event_triggers_workflow(client):
    wf = client.post("/api/workflows", json={"name": "업로드 검사", "trigger": "upload"}).json()
    data = api.engine.resolve_input("clean").read_bytes()
    response = client.post("/api/uploads", files={"file": ("new.xlsx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert response.status_code == 200, response.text
    tasks = client.get("/api/state").json()["tasks"]
    triggered = next(t for t in tasks if t["input"].get("workflow_id") == wf["id"])
    assert wait(client, triggered["id"])["status"] == "completed"


def test_due_notifications_are_real_and_deduplicated(client):
    due = (datetime.now(KST) + timedelta(minutes=1)).isoformat()
    task = client.post("/api/tasks", json={"agent": "assistant", "title": "기한 테스트", "due_at": due, "input": {"manual": True}}).json()
    client.post("/api/notifications/check")
    client.post("/api/notifications/check")
    notifications = [n for n in client.get("/api/state").json()["notifications"] if n["task_id"] == task["id"]]
    assert len(notifications) == 1
    assert client.post(f"/api/tasks/{task['id']}/resume", json={"action": "continue"}).json()["status"] == "completed"


def test_reject_invalid_upload_and_cross_origin_mutation(client):
    assert client.post("/api/uploads", files={"file": ("fake.xlsx", b"not a zip")}).status_code == 400
    assert client.post("/api/memories", json={"key": "x", "value": "y"}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/api/health").json()["mode"] == "demo"


def test_unconfigured_planner_never_calls_network(monkeypatch):
    for key in ("WORKMATE_LLM_API_KEY", "WORKMATE_LLM_BASE_URL", "WORKMATE_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    planner = Planner()
    assert planner.mode == "demo"
    assert planner.app_plan("앱") is None
    assert planner.plan("수식 검사", [], [])["action"] == "excel"
    assert planner.plan("앞으로 알림은 간결하게 해줘", [], [])["action"] == "remember"


@pytest.mark.parametrize("content", ["[]", "null"])
def test_live_planner_malformed_json_is_explicit_error(monkeypatch, content):
    import backend.llm as llm
    class Response:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": content}}]}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(llm.httpx, "Client", Client)
    p = Planner()
    p.key, p.base, p.model = "unit-test-only", "https://mock.invalid/v1", "mock"
    with pytest.raises(ValueError, match="JSON"):
        p.plan("안녕", [], [])
    with pytest.raises(ValueError, match="JSON"):
        p.app_plan("앱")
