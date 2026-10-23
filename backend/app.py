import json
import os
import re
import sys
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from .store import Store, now, uid, KST, LOCK
from .llm import Planner, load_env
from .orchestrator import Engine, AGENTS

CODE_ROOT = Path(__file__).resolve().parent.parent
# A hosted visitor receives a separately imported module, store and filesystem.
HOSTED = globals().get('_WORKMATE_HOSTED', False)
ROOT = Path(globals().get('_WORKMATE_ROOT', CODE_ROOT)).resolve()
if not HOSTED:
    load_env(ROOT / ".env")
DATA = (ROOT / 'data') if HOSTED else Path(os.environ.get("WORKMATE_DATA_DIR", str(ROOT / "data"))).resolve()
# Fixture/artifact file paths remain under the project; test store can be isolated.
store = Store(DATA / "workmate.db")
planner = Planner(inherit_env=not HOSTED)
engine = Engine(ROOT, store, planner)


@asynccontextmanager
async def lifespan(app):
    store.seed()
    from .tickets import seed_tickets
    seed_tickets(store)
    engine.start()
    yield
    engine.stop()


app = FastAPI(title="Workmate", version="0.5.0", lifespan=lifespan)
PUBLIC_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME') or os.environ.get('WORKMATE_PUBLIC_HOST', '')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"] + ([PUBLIC_HOST] if HOSTED and PUBLIC_HOST else []))


@app.middleware("http")
async def local_only_mutations(request: Request, call_next):
    origin = request.headers.get("origin")
    allowed = {"http://localhost:5173", "http://127.0.0.1:5173",
               "http://localhost:8765", "http://127.0.0.1:8765"}
    if HOSTED and PUBLIC_HOST:
        allowed = {'https://' + PUBLIC_HOST}
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in allowed:
        return JSONResponse({"detail": "이 로컬 앱에서 시작한 요청만 허용됩니다."}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    # Validation diagnostics must not echo the submitted API key.
    errors = [{"loc": list(e['loc']), "msg": e['msg'], "type": e['type']} for e in exc.errors()]
    return JSONResponse({'detail': errors}, status_code=422)


class TaskRequest(BaseModel):
    agent: str
    title: str | None = Field(default=None, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    due_at: str | None = None
    project: str = Field(default="", max_length=100)
    owner: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=6000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    priority: str = "normal"
    status: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)


class MemoryRequest(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)


class ResumeRequest(BaseModel):
    action: str = "continue"
    input: dict[str, Any] = Field(default_factory=dict)


class AppRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=6000)


class WorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=6000)
    app_id: str | None = None
    trigger: str = "manual"
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    baseline: str = "baseline"
    current: str = "clean"


class RunRequest(BaseModel):
    baseline: str | None = None
    current: str | None = None
    app_id: str | None = None


def require(collection, object_id):
    obj = store.get(collection, object_id)
    if obj is None:
        raise HTTPException(404, "항목을 찾을 수 없습니다.")
    return obj


def public_app(obj):
    return {**{k: v for k, v in obj.items() if k != "source_path"},
            "record_count": sum(r["app_id"] == obj["id"] for r in store.all("records"))}


def validate_due(value):
    if not value:
        return None
    try:
        due = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("기한은 ISO 날짜·시간 형식이어야 합니다.")
    if due.tzinfo is None:
        due = due.replace(tzinfo=KST)
    return due.astimezone(KST).isoformat(timespec="seconds")


@app.get("/api/health")
def health():
    return {"ok": True, "mode": planner.mode, "llm_configured": planner.configured, "version": "0.5.0", "service": "workmate", "hosted": HOSTED}


@app.get("/api/state")
def state():
    from .desk_api import project_summary, demo_status, REFERENCES
    all_tasks = store.all("tasks")
    tasks = [t for t in all_tasks if not t.get("archived")]
    return {"hosted": HOSTED, "mode": planner.mode, "llm_configured": planner.configured, "llm": planner.public_status(), "agents": AGENTS,
            "documents": store.all("documents"), "examples": store.all("examples"),
            "projects": project_summary(store), "demo": demo_status(store), "references": REFERENCES,
            "archived_tasks": [t for t in all_tasks if t.get("archived")],
            "chat_history": list(reversed(store.all("messages")[:100])),
            "uploads": [{k: u.get(k) for k in ("id", "name", "size", "created_at")} for u in store.all("uploads")],
            "tasks": tasks, "memories": store.all("memories"),
            "apps": [public_app(a) for a in store.all("apps")], "workflows": store.all("workflows"),
            "notifications": store.all("notifications"), "server_time": now(),
            "fixtures": [{"id": id_, "name": name, "description": desc, "filename": id_ + ".xlsx"}
                         for id_, name, desc in [
                             ("baseline", "기준 원본", "변경 전 업무 데이터"),
                             ("changed", "오류가 있는 수정본", "수식·필수값·코드 오류를 의도적으로 포함"),
                             ("clean", "보완 완료본", "정상 입력으로 재실행 확인"),
                             ("reordered", "행 순서 변경본", "ID 기준으로 행 이동 오탐 확인")]],
            "stats": {"completed": sum(t["status"] == "completed" for t in tasks),
                      "active": sum(t["status"] in ("running", "queued") for t in tasks),
                      "waiting": sum(t["status"] == "waiting_input" for t in tasks),
                      "artifacts": sum(len(t["artifacts"]) for t in tasks)}}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    return require("tasks", task_id)


@app.post("/api/tasks")
def create_task(body: TaskRequest):
    due = validate_due(body.due_at)
    if body.priority not in ("low", "normal", "high") or any(len(t) > 40 for t in body.tags):
        raise ValueError("우선순위 또는 태그를 확인하세요.")
    metadata = {k: getattr(body, k) for k in ("project", "owner", "description", "tags", "priority")}
    if body.agent == "assistant" and body.input.get("manual"):
        status = body.status or "todo"
        if status not in ("todo", "in_progress", "waiting_input", "completed"):
            raise ValueError("수동 업무 상태를 확인하세요.")
        task = {"id": uid("task"), "agent": "assistant", "title": body.title or "새 업무",
                "status": status, "created_at": now(), "updated_at": now(), "due_at": due,
                "summary": "사용자가 등록한 업무입니다. 완료 후 확인할 수 있습니다.", "input": body.input,
                "result": {}, "events": [{"at": now(), "message": "업무와 기한을 저장했습니다."}],
                "artifacts": [], **metadata, "archived": False, "mode": planner.mode,
                "completed_at": now() if status == "completed" else None}
        store.put("tasks", task)
        engine.check_notifications()
        return task
    if body.status is not None:
        raise ValueError("도구 실행 상태는 실제 실행 결과에 따라 결정됩니다.")
    return engine.submit(body.agent, body.title, body.input, due, metadata=metadata)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str, body: ResumeRequest):
    if body.action not in ("repair", "continue"):
        raise ValueError("지원하지 않는 재개 동작입니다.")
    return engine.resume(task_id, body.action, body.input)


@app.post("/api/chat")
def chat(body: ChatRequest):
    history = list(reversed(store.all("messages")[:12]))
    store.put("messages", {"id": uid("message"), "role": "user", "content": body.message, "created_at": now()})
    plan = planner.plan(body.message, store.all("memories"), store.all("apps"), history=history,
                        tasks=[t for t in store.all("tasks") if not t.get("archived")], uploads=store.all("uploads"), documents=store.all('documents'))
    def respond(reply, **extra):
        item = {"id": uid("message"), "role": "assistant", "content": reply, "created_at": now()}
        if extra.get("task_id"):
            item["task_id"] = extra["task_id"]
        store.put("messages", item)
        return {"reply": reply, **extra, "mode": planner.mode, "history": list(reversed(store.all("messages")[:100]))}
    action = plan["action"]
    if action in ("brief", "answer"):
        return respond(plan.get("reply") or engine.brief())
    if action == "remember":
        key, value = str(plan.get("key") or "사용자 메모").strip(), str(plan.get("value") or body.message).strip()
        if not key or not value or len(key) > 100 or len(value) > 2000:
            raise ValueError("기억할 항목은 제목 100자, 내용 2,000자 이내로 입력하세요.")
        memory = store.memory(key, value)
        return respond(f"기억했습니다. {key}: {value}\n기억 관리에서 수정하거나 삭제할 수 있습니다.", memory=memory)
    if action == "workflow":
        apps = store.all("apps")
        workflow = engine.workflow(plan.get("title") or "Excel 검수 및 결과 등록", body.message, apps[0]["id"] if apps else None)
        return respond("Excel 검사 → 결과 등록 절차를 저장했습니다. 자동화 화면에서 시험하고 새 파일로 다시 실행할 수 있습니다.",
                       workflow_id=workflow["id"])
    if action == "task":
        due = plan.get("due_at")
        # Demo supports explicit date/time and '2분 후'; it does not invent a date.
        if not due:
            match = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", body.message)
            minutes = re.search(r"(\d{1,3})분\s*후", body.message)
            if match:
                due = match.group(1) + "T" + match.group(2) + ":00+09:00"
            elif minutes:
                due = (datetime.now(KST) + timedelta(minutes=int(minutes.group(1)))).isoformat(timespec="seconds")
        task = create_task(TaskRequest(agent="assistant", title=plan.get("title") or body.message[:120],
                                        input={"manual": True, "message": body.message}, due_at=due, project=plan.get('project') or ''))
        return respond("업무를 저장했습니다." + (" 기한이 가까워지면 앱 알림으로 알려드립니다." if due else " 기한이 필요하면 날짜와 시간을 함께 등록해 주세요."),
                       task_id=task["id"])
    inputs = {}
    if action == "excel":
        inputs = {"baseline": plan.get("baseline") or "baseline", "current": plan.get("current") or "changed"}
        engine.resolve_input(inputs["baseline"])
        engine.resolve_input(inputs["current"])
    elif action == "builder":
        inputs = {"prompt": plan.get("prompt") or body.message}
    elif action == "developer":
        apps = store.all("apps")
        app_id = plan.get("app_id") or (apps[0]["id"] if apps else None)
        if not app_id:
            return respond("먼저 업무 앱을 만들어 주세요. 생성한 앱에 기능 변경과 실제 UAT를 실행할 수 있습니다.")
        if not store.get("apps", app_id):
            raise ValueError("요청 해석 결과의 대상 앱을 찾을 수 없습니다.")
        inputs = {"app_id": app_id, "requirement": plan.get("prompt") or body.message}
    elif action == 'knowledge':
        inputs = {'question': plan.get('question') or body.message, 'document_ids': plan.get('document_ids') or [], 'project': plan.get('project') or ''}
    elif action == 'analyst':
        if not plan.get('before_id') or not plan.get('after_id'):
            return respond('자료 분석 화면에서 변경 전·후 문서를 선택하면 차이와 후속 업무를 분석할 수 있습니다.')
        inputs = {'before_id': plan['before_id'], 'after_id': plan['after_id'], 'question': plan.get('question') or body.message}
    elif action == 'reporter':
        inputs = {'project': plan.get('project') or '', 'period_days': 7, 'report_type': plan.get('report_type') or 'weekly'}
    task = engine.submit(action, plan.get("title"), inputs, metadata={'project': plan.get('project') or ''})
    description = "Excel 검사를 시작했습니다." if action == "excel" else "작업을 접수했습니다. 실행 상태와 결과물을 작업 화면에서 확인할 수 있습니다."
    return respond(description, task_id=task["id"])


@app.post("/api/memories")
def add_memory(body: MemoryRequest):
    if not body.key.strip() or not body.value.strip():
        raise ValueError("기억의 제목과 내용을 입력하세요.")
    return store.memory(body.key.strip(), body.value.strip())


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    require("memories", memory_id)
    store.delete("memories", memory_id)
    return {"ok": True}


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise ValueError(".xlsx 파일만 업로드할 수 있습니다.")
    blob = await file.read(10 * 1024 * 1024 + 1)
    if len(blob) > 10 * 1024 * 1024:
        raise ValueError("파일 크기는 10 MB 이하여야 합니다.")
    from io import BytesIO
    try:
        with zipfile.ZipFile(BytesIO(blob)) as z:
            if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024 or len(z.infolist()) > 2000:
                raise ValueError("워크북의 압축 해제 크기 또는 내부 파일 수가 제한을 초과합니다.")
            if "xl/workbook.xml" not in z.namelist():
                raise ValueError("Excel 워크북 구조가 아닙니다.")
            if any("vbaproject" in i.filename.lower() for i in z.infolist()):
                raise ValueError("매크로가 포함된 워크북은 이 버전에서 지원하지 않습니다.")
    except zipfile.BadZipFile:
        raise ValueError("유효한 .xlsx 파일이 아닙니다.")
    upload_id = uid("upload")
    path = ROOT / "data" / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    (path / (upload_id + ".xlsx")).write_bytes(blob)
    obj = store.put("uploads", {"id": upload_id, "name": Path(file.filename).name, "filename": upload_id + ".xlsx",
                                "size": len(blob), "created_at": now()})
    for workflow in store.all("workflows"):
        if workflow.get("enabled") and workflow.get("trigger") == "upload":
            engine.submit("automation", workflow["name"], {"workflow_id": workflow["id"], "baseline": workflow.get("baseline", "baseline"),
                                                         "current": upload_id, "app_id": workflow.get("app_id")})
    return {"id": obj["id"], "name": obj["name"]}


@app.get("/api/fixtures/{fixture_id}")
def fixture(fixture_id: str):
    if fixture_id not in ("baseline", "changed", "clean", "reordered"):
        raise HTTPException(404, "샘플을 찾을 수 없습니다.")
    return FileResponse(engine.resolve_input(fixture_id), filename=fixture_id + ".xlsx")


@app.post("/api/apps")
def create_business_app(body: AppRequest):
    return engine.submit("builder", "업무 앱 제작", {"prompt": body.prompt})


@app.get("/api/apps/{app_id}")
def get_app(app_id: str):
    return public_app(require("apps", app_id))


@app.get("/api/apps/{app_id}/records")
def get_records(app_id: str):
    require("apps", app_id)
    return [r for r in store.all("records") if r["app_id"] == app_id]


@app.post("/api/apps/{app_id}/records")
def create_record(app_id: str, body: dict):
    business_app = require("apps", app_id)
    fields = {f["name"] for f in business_app["schema"]["fields"]}
    values = {k: v for k, v in body.items() if k in fields}
    engine.validate_record(business_app, values)
    return store.put("records", {"id": uid("record"), "app_id": app_id, **values, "created_at": now()})


@app.patch("/api/apps/{app_id}/records/{record_id}")
def update_record(app_id: str, record_id: str, body: dict):
    with LOCK:
        business_app = require("apps", app_id)
        record = require("records", record_id)
        if record["app_id"] != app_id:
            raise HTTPException(404, "이 앱의 데이터가 아닙니다.")
        fields = {f["name"] for f in business_app["schema"]["fields"]}
        values = {**record, **{k: v for k, v in body.items() if k in fields}}
        engine.validate_record(business_app, values)
        return store.put("records", {**values, "updated_at": now()})


@app.get("/api/apps/{app_id}/view")
def app_view(app_id: str):
    obj = require("apps", app_id)
    path = Path(obj["source_path"]).resolve()
    if not path.is_relative_to((ROOT / "data" / "artifacts").resolve()) or not path.is_file():
        raise HTTPException(404, "실행할 앱 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.post("/api/workflows")
def create_workflow(body: WorkflowRequest):
    if body.trigger not in ("manual", "upload", "interval"):
        raise ValueError("수동, 파일 업로드, 주기 실행 중 선택하세요.")
    engine.resolve_input(body.baseline)
    engine.resolve_input(body.current)
    obj = engine.workflow(body.name, body.prompt, body.app_id)
    return store.update("workflows", obj["id"], trigger=body.trigger, baseline=body.baseline, current=body.current,
                        interval_minutes=body.interval_minutes,
                        next_run_at=(datetime.now(KST) + timedelta(minutes=body.interval_minutes)).isoformat(timespec="seconds") if body.trigger == "interval" else None)


@app.post("/api/workflows/{workflow_id}/run")
def run_workflow(workflow_id: str, body: RunRequest):
    workflow = require("workflows", workflow_id)
    if not workflow.get("enabled", True):
        raise ValueError("일시정지된 자동화입니다. 실행을 재개한 뒤 시도하세요.")
    inputs = {"workflow_id": workflow_id, "baseline": workflow.get("baseline", "baseline"),
              "current": workflow.get("current", "clean"), "app_id": workflow.get("app_id")}
    inputs.update(body.model_dump(exclude_unset=True, exclude_none=True))
    return engine.submit("automation", workflow["name"], inputs)


@app.post("/api/notifications/check")
def check_notifications():
    return {"created": engine.check_notifications()}


@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str):
    require("notifications", notification_id)
    store.update("notifications", notification_id, read=True)
    return {"ok": True}


@app.get("/api/artifacts/{task_id}/{filename:path}")
def artifact(task_id: str, filename: str):
    task = require("tasks", task_id)
    folder = (ROOT / "data" / "artifacts" / task_id).resolve()
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise HTTPException(404, "결과물을 찾을 수 없습니다.")
    # Serve only artifacts explicitly associated with this task, never arbitrary server files.
    if filename not in {a["name"] for a in task["artifacts"]}:
        raise HTTPException(404, "등록된 결과물이 아닙니다.")
    if path.suffix.lower() in (".html", ".png", ".jpg", ".json", ".txt"):
        return FileResponse(path)
    return FileResponse(path, filename=path.name)


from .connection import install_connection
install_connection(app, sys.modules[__name__])

from .features import install_features
install_features(app, sys.modules[__name__])
from .desk_api import install_desk
install_desk(app, sys.modules[__name__])
from .companion import install_companion
from .tickets import install_tickets
install_companion(app, sys.modules[__name__])
install_tickets(app, sys.modules[__name__])

DIST = CODE_ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
else:
    @app.get("/")
    def missing_frontend():
        return {"message": "프런트엔드를 빌드하세요: cd frontend && npm install && npm run build", "api": "/docs"}
