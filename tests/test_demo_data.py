import copy
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend import demo_data
from backend.demo_data import COLLECTIONS, LABEL, PREFIX, seed_demo
from backend.orchestrator import Engine
from backend.store import KST, Store


EXPECTED_COUNTS = {
    "projects": 5, "tasks": 40, "documents": 10, "memories": 5,
    "notifications": 6, "apps": 4, "records": 48, "workflows": 3, "examples": 14,
}


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data" / "test.db")
    return tmp_path, store


@pytest.fixture
def seeded(workspace):
    root, store = workspace
    result = seed_demo(root, store)
    return root, store, result


def test_seed_creates_labelled_data_and_real_apps_without_model_or_uat(workspace, monkeypatch):
    from backend import builder, llm

    root, store = workspace
    generate = Mock(wraps=demo_data.create_app)
    llm_call = Mock(side_effect=AssertionError("Seeding must not call a model"))
    uat_call = Mock(side_effect=AssertionError("Seeding must not invent or execute a UAT run"))
    monkeypatch.setattr(demo_data, "create_app", generate)
    monkeypatch.setattr(llm.Planner, "_complete", llm_call)
    monkeypatch.setattr(builder, "develop_app", uat_call)
    result = seed_demo(root, store)
    assert result["created_counts"] == EXPECTED_COUNTS
    assert result["totals"] == EXPECTED_COUNTS
    assert result["created_total"] == 135
    assert result["warnings"] == []
    assert generate.call_count == 4
    assert all(isinstance(call.kwargs.get("plan"), dict) for call in generate.call_args_list)
    llm_call.assert_not_called()
    uat_call.assert_not_called()
    for collection in COLLECTIONS:
        for item in store.all(collection):
            assert item["id"].startswith(PREFIX)
            assert item["synthetic"] is True
            assert item["sourceLabel"] == LABEL


def test_manual_tasks_have_relative_kst_dates_and_explicit_synthetic_completion(seeded):
    _, store, result = seeded
    anchor = datetime.fromisoformat(result["seed_anchor_at"])
    projects = {project["name"] for project in store.all("projects")}
    tasks = store.all("tasks")
    assert Counter(task["status"] for task in tasks) == {
        "todo": 10, "in_progress": 10, "waiting_input": 10, "completed": 10,
    }
    offsets = set()
    for task in tasks:
        due = datetime.fromisoformat(task["due_at"])
        assert due.utcoffset() == anchor.astimezone(KST).utcoffset()
        offsets.add((due.date() - anchor.date()).days)
        assert task["project"] in projects
        assert task["owner"] and task["tags"]
        assert task["priority"] in {"high", "normal", "low"}
        assert task["agent"] == "assistant" and task["input"]["manual"] is True
        assert task["artifacts"] == [] and task["result"] == {} and task["attempt"] == 0
        assert task["summary"] and task["sourceLabel"] == LABEL
        if task["status"] == "completed":
            completed = datetime.fromisoformat(task["completed_at"])
            assert anchor - completed < timedelta(days=7)
            assert completed <= anchor
            assert task["events"]
            assert task["events"][0]["at"] == task["completed_at"]
            assert task["events"][0]["synthetic"] is True
            assert task["events"][0]["message"] == f"업무 상태: 완료 · {task['title']}"
        else:
            assert task["completed_at"] is None
            assert task["events"] == []
    assert min(offsets) == -2 and max(offsets) == 12


def test_repeated_seed_preserves_user_data_edits_and_generated_source(seeded):
    root, store, _ = seeded
    user_task = {
        "id": "user_actual_task", "title": "사용자의 실제 작업", "status": "running",
        "input": {}, "result": {"actual_result": "keep"}, "events": [{"message": "실제 기록"}],
        "artifacts": [{"name": "original.txt"}],
    }
    store.put("tasks", copy.deepcopy(user_task))
    store.put("memories", {"id": "user_memory", "key": "사용자 원래 메모", "value": "그대로 유지"})
    edited_id = PREFIX + "task_erp_01"
    store.update("tasks", edited_id, title="사용자가 편집한 업무", status="todo", priority="low")
    app_id = PREFIX + "app_requests"
    store.update("apps", app_id, name="사용자가 바꾼 앱 이름", version=4)
    source = Path(store.get("apps", app_id)["source_path"])
    source.write_text(source.read_text(encoding="utf-8") + "\n<!-- preserved user edit -->", encoding="utf-8")
    before_source = source.read_bytes()
    before = {collection: store.all(collection) for collection in COLLECTIONS}

    repeated = seed_demo(root, store)
    assert repeated["created_total"] == 0
    assert all(count == 0 for count in repeated["created_counts"].values())
    assert store.get("tasks", "user_actual_task") == user_task
    assert {collection: store.all(collection) for collection in COLLECTIONS} == before
    assert source.read_bytes() == before_source


def test_seed_does_not_insert_records_into_existing_user_app_id(workspace):
    root, store = workspace
    user_app = {"id": PREFIX + "app_requests", "name": "기존 사용자 앱", "schema": {"fields": []}, "version": 9}
    store.put("apps", copy.deepcopy(user_app))
    result = seed_demo(root, store)
    assert store.get("apps", user_app["id"]) == user_app
    assert not any(record["app_id"] == user_app["id"] for record in store.all("records"))
    assert result["created_counts"]["apps"] == 3
    assert result["created_counts"]["records"] == 36
    assert any("기존 사용자 앱" in warning for warning in result["warnings"])


def test_existing_managed_folder_is_not_overwritten(workspace):
    root, store = workspace
    source = root / "data" / "artifacts" / "demo_v3_apps" / "requests" / "app.html"
    source.parent.mkdir(parents=True)
    source.write_text("user-owned existing content", encoding="utf-8")
    result = seed_demo(root, store)
    assert source.read_text(encoding="utf-8") == "user-owned existing content"
    assert store.get("apps", PREFIX + "app_requests") is None
    assert any("덮어쓰지" in warning for warning in result["warnings"])


def test_documents_are_rich_paragraph_sources_with_real_download_files(seeded):
    root, store, result = seeded
    documents = store.all("documents")
    assert len(documents) == 10
    assert len(result["document_files"]) == 10
    for document in documents:
        paragraphs = [paragraph for paragraph in document["body"].split("\n\n") if paragraph.strip()]
        assert len(paragraphs) >= 5
        assert len(document["body"]) >= 600
        assert paragraphs[0].startswith("[업무 예제]")
        assert document["paragraph_count"] == len(paragraphs)
        path = Path(document["source_path"])
        assert path.resolve().is_relative_to((root / "data" / "examples").resolve())
        assert path.read_text(encoding="utf-8") == document["body"]
    for before, after in (("requirements_v1", "requirements_v2"), ("finance_v1", "finance_v2")):
        assert store.get("documents", PREFIX + "doc_" + before)["body"] != store.get("documents", PREFIX + "doc_" + after)["body"]
    assert (root / "data" / "examples" / "demo_v3_erp_cutover.txt").is_file()


def test_all_seed_records_satisfy_the_actual_server_schema(seeded):
    root, store, _ = seeded
    assert Counter(record["app_id"] for record in store.all("records")) == {
        PREFIX + "app_" + key: 12 for key in ("requests", "settlements", "quotes", "risks")
    }
    for business_app in store.all("apps"):
        source = Path(business_app["source_path"])
        assert source.resolve().is_relative_to((root / "data" / "artifacts" / "demo_v3_apps").resolve())
        assert business_app["id"] in source.read_text(encoding="utf-8")
        assert json.loads(source.with_name("app-schema.json").read_text(encoding="utf-8")) == business_app["schema"]
        assert business_app["verification_status"] == "not_tested"
        assert not list(source.parent.glob("evidence-*.png"))
        assert len(business_app["schema"]["fields"]) > 5
        for record in store.all("records"):
            if record["app_id"] == business_app["id"]:
                values = {field["name"]: copy.deepcopy(record.get(field["name"])) for field in business_app["schema"]["fields"]}
                Engine.validate_record(business_app, values)
    assert any(record.get("effort_hours") == 0 for record in store.all("records"))


def test_seed_respects_new_required_fields_in_an_existing_demo_app(seeded):
    root, store, _ = seeded
    app_id = PREFIX + "app_requests"
    business_app = store.get("apps", app_id)
    business_app["schema"]["fields"].append({"name": "customer_ref", "label": "고객 참조", "type": "text", "required": True})
    store.put("apps", business_app)
    missing = PREFIX + "record_requests_01"
    store.delete("records", missing)
    result = seed_demo(root, store)
    assert store.get("records", missing) is None
    assert store.get("apps", app_id)["schema"] == business_app["schema"]
    assert any(missing in warning for warning in result["warnings"])


def test_workflows_are_disabled_and_notifications_are_deduplicated(seeded):
    root, store, _ = seeded
    for workflow in store.all("workflows"):
        assert workflow["enabled"] is False
        assert workflow["trigger"] in {"manual", "interval"}
        assert workflow["next_run_at"] is None and workflow["last_run_at"] is None
    before = store.all("notifications")
    assert len(before) == 6
    assert all(notification["sourceLabel"] == LABEL and notification["message"] for notification in before)
    assert all(store.get("tasks", notification["task_id"]) is not None for notification in before)
    seed_demo(root, store)
    assert store.all("notifications") == before


def test_catalog_inputs_reference_seeded_resources_and_expected_agents(seeded):
    _, store, result = seeded
    examples = result["examples"]
    assert Counter(example["agent"] for example in examples) == {
        "excel": 3, "knowledge": 3, "analyst": 2, "reporter": 2,
        "builder": 1, "developer": 1, "automation": 1, "assistant": 1,
    }
    projects = {project["name"] for project in store.all("projects")}
    for example in examples:
        assert example["project"] in projects or example['id'] == 'demo_v3_example_reporter_all' and example['project'] == ''
        assert example["expected"] and example["description"] and example["difficulty"]
        assert isinstance(example["uses_llm"], bool)
        assert isinstance(example["estimated_seconds"], int) and example["estimated_seconds"] > 0
        inputs = example["input"]
        if example["agent"] == "knowledge":
            assert inputs["question"]
            assert all(store.get("documents", doc_id) for doc_id in inputs["document_ids"])
        elif example["agent"] == "analyst":
            assert store.get("documents", inputs["before_id"])
            assert store.get("documents", inputs["after_id"])
            assert inputs["before_id"] != inputs["after_id"]
        elif example["agent"] == "reporter":
            assert inputs["period_days"] == 7
        elif example["agent"] in {"automation", "developer"}:
            assert store.get("apps", inputs["app_id"])
            if example["agent"] == "automation":
                assert store.get("workflows", inputs["workflow_id"])
                values = {"title": "예제 검수", "owner": "담당자", "status": "완료", "completion_note": "업무 예제", **inputs["record_values"]}
                Engine.validate_record(store.get("apps", inputs["app_id"]), values)
        elif example["agent"] == "assistant":
            assert inputs["manual"] is True


def test_concurrent_seed_requests_create_only_one_set(workspace):
    root, store = workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(seed_demo, root, store) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(result["created_total"] for result in results) == [0, 135]
    assert all(result["totals"] == EXPECTED_COUNTS for result in results)
    assert all(result["warnings"] == [] for result in results)
