"""User-facing task/file/workflow management, exports and connection diagnostics."""
import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from .store import now, KST, LOCK


def safe_csv(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: str | None = None
    priority: str | None = None
    archived: bool | None = None
    project: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=10)
    description: str | None = Field(default=None, max_length=6000)
    status: str | None = None


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=6000)
    enabled: bool | None = None
    trigger: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    baseline: str | None = None
    current: str | None = None
    app_id: str | None = None


def install_features(app, api):
    @app.post("/api/connection/test")
    def connection_test():
        return api.planner.test_connection()

    @app.get("/api/chat/history")
    def chat_history():
        return list(reversed(api.store.all("messages")[:100]))

    @app.delete("/api/chat/history")
    def clear_history():
        for message in api.store.all("messages"):
            api.store.delete("messages", message["id"])
        return {"ok": True}

    @app.patch("/api/tasks/{task_id}")
    def edit_task(task_id: str, body: TaskPatch):
        with LOCK:
            task = api.require("tasks", task_id)
            changes = body.model_dump(exclude_unset=True)
            if "title" in changes and (not changes["title"] or not changes["title"].strip()):
                raise ValueError("업무 제목을 입력하세요.")
            if "priority" in changes and changes["priority"] not in ("low", "normal", "high"):
                raise ValueError("우선순위를 확인하세요.")
            if "tags" in changes and (changes["tags"] is None or any(len(v) > 40 for v in changes["tags"])):
                raise ValueError("태그는 각각 40자 이내로 입력하세요.")
            if "status" in changes:
                if not task["input"].get("manual") or changes["status"] not in ("todo", "in_progress", "waiting_input", "completed"):
                    raise ValueError("개인 업무만 할 일·진행 중·확인 필요·완료로 변경할 수 있습니다.")
                changes["completed_at"] = (task.get("completed_at") if task["status"] == "completed" else now()) if changes["status"] == "completed" else None
                changes["summary"] = {"todo": "등록한 할 일입니다.", "in_progress": "사용자가 진행 중으로 표시했습니다.", "waiting_input": "확인 또는 협의가 필요한 업무입니다.", "completed": "사용자가 완료를 확인했습니다."}[changes["status"]]
            if "due_at" in changes:
                changes["due_at"] = api.validate_due(changes["due_at"])
            if changes.get("archived") and task["status"] in ("running", "queued"):
                raise ValueError("실행이 끝난 뒤 보관하세요.")
            changes["updated_at"] = now()
            result = api.store.update("tasks", task_id, **changes)
            if "status" in changes:
                api.store.event(task_id, changes["summary"])
            api.engine.check_notifications()
            return result

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        task = api.require("tasks", task_id)
        if task["status"] in ("running", "queued") or task["input"].get("manual"):
            raise ValueError("이 업무는 재실행할 수 없습니다.")
        result = api.engine.submit(task["agent"], task["title"], task["input"], task.get("due_at"), metadata=task)
        return api.store.update("tasks", result["id"], parent_id=task_id, priority=task.get("priority", "normal"))

    @app.get("/api/tasks/{task_id}/bundle")
    def task_bundle(task_id: str):
        task = api.require("tasks", task_id)
        root = (api.ROOT / "data" / "artifacts" / task_id).resolve()
        files = []
        for artifact in task["artifacts"]:
            path = (root / artifact["name"]).resolve()
            if path.is_relative_to(root) and path.is_file():
                files.append((path, path.relative_to(root).as_posix()))
        if sum(path.stat().st_size for path, _ in files) > 100 * 1024 * 1024:
            raise ValueError("한 번에 내보낼 수 있는 결과물 크기를 초과했습니다.")
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("task.json", json.dumps(task, ensure_ascii=False, indent=2))
            for path, name in files:
                archive.write(path, name)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'})

    @app.get("/api/uploads/{upload_id}")
    def download_upload(upload_id: str):
        upload = api.require("uploads", upload_id)
        return FileResponse(api.engine.resolve_input(upload_id), filename=upload["name"])

    @app.delete("/api/apps/{app_id}/records/{record_id}")
    def delete_record(app_id: str, record_id: str):
        api.require("apps", app_id)
        record = api.require("records", record_id)
        if record["app_id"] != app_id:
            raise HTTPException(404, "이 앱의 데이터가 아닙니다.")
        api.store.delete("records", record_id)
        return {"ok": True}

    @app.get("/api/apps/{app_id}/export.csv")
    def export_app(app_id: str):
        business_app = api.require("apps", app_id)
        fields = business_app["schema"]["fields"]
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow([safe_csv(f["label"]) for f in fields])
        for record in api.store.all("records"):
            if record["app_id"] == app_id:
                writer.writerow([safe_csv(record.get(f["name"])) for f in fields])
        return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{app_id}.csv"'})

    @app.patch("/api/workflows/{workflow_id}")
    def edit_workflow(workflow_id: str, body: WorkflowPatch):
        with LOCK:
            workflow = api.require("workflows", workflow_id)
            changes = body.model_dump(exclude_unset=True)
            if any(value is None for key, value in changes.items() if key != "app_id"):
                raise ValueError("자동화 설정값을 비워 둘 수 없습니다.")
            if "name" in changes and not changes["name"].strip():
                raise ValueError("자동화 이름을 입력하세요.")
            if "prompt" in changes:
                changes["description"] = changes.pop("prompt")
            if "trigger" in changes and changes["trigger"] not in ("manual", "upload", "interval"):
                raise ValueError("수동, 파일 업로드, 주기 실행 중 선택하세요.")
            if changes.get("app_id"):
                api.require("apps", changes["app_id"])
            for field in ("baseline", "current"):
                if field in changes:
                    api.engine.resolve_input(changes[field])
            merged = {**workflow, **changes}
            if merged.get("trigger") == "interval":
                interval = merged.get("interval_minutes") or 60
                changes["interval_minutes"] = interval
                if any(k in changes for k in ("interval_minutes", "trigger", "enabled")):
                    changes["next_run_at"] = (datetime.now(KST) + timedelta(minutes=interval)).isoformat(timespec="seconds")
            else:
                changes["next_run_at"] = None
            return api.store.update("workflows", workflow_id, **changes)

    @app.delete("/api/workflows/{workflow_id}")
    def delete_workflow(workflow_id: str):
        with LOCK:
            workflow = api.require("workflows", workflow_id)
            tasks = [t for t in api.store.all("tasks") if t["input"].get("workflow_id") == workflow_id]
            if any(t["status"] in ("queued", "running") for t in tasks):
                raise ValueError("실행 중인 작업이 끝나면 자동화를 삭제할 수 있습니다.")
            for task in tasks:
                if task["status"] == "waiting_input" and not task["input"].get("workflow_snapshot"):
                    api.store.update("tasks", task["id"], input={**task["input"], "workflow_snapshot": workflow})
            api.store.delete("workflows", workflow_id)
        return {"ok": True}
