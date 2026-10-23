import hashlib
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from .store import Store, now, uid, KST, LOCK

AGENTS = [
    {"id": "assistant", "name": "개인 업무비서", "description": "업무를 기억하고, 우선순위를 정하고, 실행을 연결합니다."},
    {"id": "excel", "name": "Excel 품질 검증", "description": "파일 변경과 수식·데이터 오류를 근거와 함께 확인합니다."},
    {"id": "builder", "name": "업무 앱 제작", "description": "설명으로 데이터를 저장하는 업무 도구를 만듭니다."},
    {"id": "developer", "name": "개발 · UAT", "description": "기능 변경부터 실제 화면 테스트와 UAT까지 연결합니다."},
    {"id": "automation", "name": "업무 자동화", "description": "검수 절차를 저장하고 새 자료에 다시 실행합니다."},
    {"id": "knowledge", "name": "자료 근거 검색", "description": "저장된 자료에서 근거 문단과 함께 답을 찾습니다."},
    {"id": "analyst", "name": "변경 영향 분석", "description": "문서 두 버전의 차이와 확인할 후속 업무를 정리합니다."},
    {"id": "reporter", "name": "프로젝트 보고", "description": "실제 업무 기록으로 주간보고·인수인계·회의 준비 자료를 만듭니다."},
]


class Engine:
    def __init__(self, root, store: Store, planner):
        self.root, self.store, self.planner = Path(root), store, planner
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="workmate")
        self.stopping = threading.Event()

    def start(self):
        for run in self.store.all('uat_runs'):
            if run.get('status') in ('running', 'queued'):
                self.store.update('uat_runs', run['id'], status='failed', error='서버 재시작으로 중단되었습니다. 다시 실행하세요.', passed=False)
        for task in self.store.all("tasks"):
            if task["status"] in ("running", "queued"):
                self.store.update("tasks", task["id"], status="failed", summary="서버 재시작으로 실행이 중단되었습니다. 새 작업으로 다시 실행하세요.", updated_at=now())
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)
        self.scheduler.start()

    def stop(self):
        self.stopping.set()
        self.pool.shutdown(wait=False)

    def _schedule(self):
        while not self.stopping.wait(15):
            try:
                self.check_notifications()
                from .companion import tick
                tick(self.store)
                self.check_due_workflows()
            except Exception:
                # A notification failure never changes task truth.
                continue

    def resolve_input(self, name):
        fixture_names = {"baseline": "baseline.xlsx", "changed": "changed.xlsx", "clean": "clean.xlsx", "reordered": "reordered.xlsx"}
        if name in fixture_names:
            path = self.root / "data" / "fixtures" / fixture_names[name]
        else:
            upload = self.store.get("uploads", str(name))
            if upload is None:
                raise ValueError("선택한 파일을 찾을 수 없습니다.")
            path = self.root / "data" / "uploads" / upload["filename"]
        if not path.is_file():
            raise ValueError("샘플 파일이 준비되지 않았습니다. 샘플 생성 명령을 확인하세요.")
        return path

    def submit(self, agent, title=None, inputs=None, due_at=None, metadata=None):
        if agent not in {a["id"] for a in AGENTS}:
            raise ValueError("지원하지 않는 에이전트입니다.")
        inputs = dict(inputs or {})
        declared_project = (metadata or {}).get('project') or ''
        input_project = inputs.get('project') or ''
        if declared_project and input_project and declared_project != input_project:
            raise ValueError('작업 프로젝트와 실행 입력의 프로젝트가 일치해야 합니다.')
        if declared_project:
            inputs['project'] = declared_project
        if agent == "automation" and inputs.get("workflow_id"):
            workflow = self.store.get("workflows", inputs["workflow_id"])
            if workflow is None:
                raise ValueError("자동화 정의를 찾을 수 없습니다.")
            inputs = {**inputs, "workflow_snapshot": workflow}
        task = {"id": uid("task"), "agent": agent, "title": title or next(a["name"] for a in AGENTS if a["id"] == agent),
                "status": "queued", "created_at": now(), "updated_at": now(), "due_at": due_at,
                "summary": "실행 대기 중", "input": inputs, "result": {}, "events": [], "artifacts": [],
                "priority": "normal", "archived": False, "mode": self.planner.mode}
        task.update({k: v for k, v in (metadata or {}).items() if k in {"project", "owner", "tags", "description", "priority", "synthetic", "sourceLabel", "example_id"}})
        if not task.get("project") and inputs.get("project"):
            task["project"] = str(inputs["project"])[:100]
        if not task.get("project") and agent in ("knowledge", "analyst"):
            ids = inputs.get('document_ids') if agent == 'knowledge' else [inputs.get('before_id'), inputs.get('after_id')]
            if isinstance(ids, list) and ids:
                docs = [self.store.get('documents', identifier) for identifier in ids if isinstance(identifier, str)]
                projects = {doc.get('project') for doc in docs if doc and doc.get('project')}
                if len(projects) == 1:
                    task['project'] = projects.pop()
        if not task.get('project') and inputs.get('app_id'):
            target_app = self.store.get('apps', inputs['app_id'])
            if target_app and target_app.get('project'):
                task['project'] = target_app['project']
        if task.get('project'):
            inputs['project'] = task['project']
        self.store.put("tasks", task)
        self.pool.submit(self.run, task["id"])
        return task

    def artifacts(self, task_id, folder, items):
        result = []
        for item in items:
            name = item["name"] if isinstance(item, dict) else str(item)
            target = (folder / name).resolve()
            if not target.is_relative_to(folder.resolve()) or not target.is_file():
                continue
            relative = target.relative_to(self.root / "data" / "artifacts" / task_id).as_posix()
            result.append({"id": uid("artifact"), "name": relative,
                           "kind": item.get("kind", target.suffix.lstrip(".")) if isinstance(item, dict) else target.suffix.lstrip("."),
                           "url": f"/api/artifacts/{task_id}/{quote(relative)}"})
        return result

    def save_result(self, task_id, result, folder, status="completed", summary=None):
        existing = self.store.get("tasks", task_id)["artifacts"]
        additions = self.artifacts(task_id, folder, result.get("artifacts", []))
        by_name = {a["name"]: a for a in [*existing, *additions]}
        self.store.update("tasks", task_id, result=result, status=status, updated_at=now(),
                          completed_at=now() if status == "completed" else None,
                          summary=summary or result.get("summary", "실행이 완료되었습니다."),
                          artifacts=list(by_name.values()))
        self.store.event(task_id, summary or result.get("summary", "실행이 완료되었습니다."))
        if status in ("completed", "waiting_input", "failed"):
            self.store.notify(f"{task_id}:{status}:{len(self.store.get('tasks', task_id)['events'])}",
                              "확인이 필요합니다" if status == "waiting_input" else "작업 결과",
                              self.store.get("tasks", task_id)["summary"], task_id)

    def run(self, task_id):
        task = self.store.get("tasks", task_id)
        attempt = task.get("attempt", 0) + 1
        folder = self.root / "data" / "artifacts" / task_id / f"attempt-{attempt:02}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.store.update("tasks", task_id, status="running", updated_at=now(), attempt=attempt)
            self.store.event(task_id, "실제 도구 실행을 시작했습니다.")
            inputs = task["input"]
            if task["agent"] == "excel":
                from .excel_agent import inspect_workbook, inspect_workbooks
                baseline = self.resolve_input(inputs.get("baseline", "baseline"))
                if inputs.get("currents"):
                    if not isinstance(inputs["currents"], list) or not 1 <= len(inputs["currents"]) <= 10:
                        raise ValueError("일괄 검사는 1~10개 파일을 선택하세요.")
                    result = inspect_workbooks(baseline, [self.resolve_input(value) for value in inputs["currents"]], folder)
                else:
                    result = inspect_workbook(baseline, self.resolve_input(inputs.get("current", "changed")), folder)
                self.save_result(task_id, result, folder, "failed" if result.get("status") == "failed" else "completed")
            elif task["agent"] == "builder":
                from .builder import create_app
                app_id = uid("app")
                prompt = str(inputs.get("prompt", "고객 요청을 담당자와 기한, 상태로 관리하는 앱"))
                plan = self.planner.app_plan(prompt)
                result = create_app(prompt, app_id, folder, plan=plan)
                app = {"id": app_id, "name": result["name"], "description": result["description"],
                       "schema": result["schema"], "source_path": str(folder / result["source_filename"]),
                       "created_at": now(), "version": 1, "url": f"/api/apps/{app_id}/view",
                       "project": task.get('project', ''), "synthetic": bool(task.get('synthetic'))}
                self.store.put("apps", app)
                result["app_id"] = app_id
                result["summary"] = f"{app['name']} 앱을 만들었습니다. 입력한 데이터는 서버에 저장됩니다."
                self.save_result(task_id, result, folder)
            elif task["agent"] == "developer":
                from .builder import develop_app
                app = self.store.get("apps", inputs.get("app_id", ""))
                if app is None:
                    self.save_result(task_id, {"summary": "변경할 앱을 먼저 만들어 주세요.", "artifacts": []},
                                     folder, "waiting_input")
                    return
                requirement = str(inputs.get("requirement", "완료 처리 시 처리 내용을 필수로 입력하게 해줘"))
                self.store.event(task_id, "제안 코드를 만들고 실제 브라우저에서 입력·저장·검증을 확인합니다.")
                plan = self.planner.app_plan(requirement, app["schema"])
                result = develop_app(app, requirement, folder, plan=plan)
                if result.get("passed"):
                    with LOCK:
                        current_app = self.store.get("apps", app["id"])
                        if current_app["version"] != app["version"]:
                            result["applied"] = False
                            result["summary"] = "테스트 중 다른 작업이 앱을 변경했습니다. 기존 변경을 보존했으며 최신 버전에서 다시 요청하세요."
                            self.save_result(task_id, result, folder, "failed")
                            return
                        self.store.update("apps", app["id"], schema=result["schema"],
                                          source_path=str(folder / result["source_filename"]), version=app["version"] + 1)
                        result["applied"] = True
                        self.store.event(task_id, "브라우저 검증을 통과한 변경을 앱에 반영했습니다.")
                result["app_id"] = app["id"]
                self.save_result(task_id, result, folder, "completed" if result.get("passed") else "failed")
            elif task["agent"] == "automation":
                self.run_workflow(task_id, folder)
            elif task["agent"] in ("knowledge", "analyst", "reporter"):
                from .desk_agent import run_desk
                result = run_desk(task["agent"], {**inputs, '_task_id': task_id}, self.root, self.store, self.planner, folder)
                self.save_result(task_id, result, folder)
            else:
                self.save_result(task_id, {"reply": self.brief(), "summary": "업무 기록을 확인했습니다.", "artifacts": []}, folder)
        except Exception as exc:
            # Never expose credentials or remote response bodies to users.
            message = str(exc)[:1000]
            self.store.update("tasks", task_id, status="failed", summary=message, updated_at=now())
            self.store.event(task_id, "실행 실패: " + message)
            self.store.notify(task_id + ":failure", "작업 실행 실패", message, task_id)

    def run_workflow(self, task_id, folder):
        from .excel_agent import inspect_workbook
        task = self.store.get("tasks", task_id)
        inputs = task["input"]
        workflow = inputs.get("workflow_snapshot") or self.store.get("workflows", inputs.get("workflow_id", ""))
        if workflow is None:
            raise ValueError("자동화 정의를 찾을 수 없습니다.")
        baseline = self.resolve_input(inputs.get("baseline", "baseline"))
        current = self.resolve_input(inputs.get("current", "changed"))
        result = inspect_workbook(baseline, current, folder)
        self.store.event(task_id, "1/2 Excel 검사를 실제 실행했습니다.")
        # All findings block registration; 'unverified' never means valid.
        if result.get("findings") or result.get("supported_schema") is False:
            result["summary"] = "검수에서 확인할 항목을 발견했습니다. 수정 사본 생성 또는 보완 파일 선택 후 재개하세요."
            result["checkpoint"] = "validation"
            self.save_result(task_id, result, folder, "waiting_input")
            return
        app_id = inputs.get("app_id") or workflow.get("app_id")
        if app_id:
            app = self.store.get("apps", app_id)
            if not app:
                raise ValueError("등록할 앱이 없습니다. 자동화의 대상 앱을 확인하세요.")
            # Idempotent registration: repeating/resuming this task cannot duplicate its record.
            record_id = "record_" + hashlib.sha256((workflow["id"] + ":" + app_id + ":" + hashlib.sha256(current.read_bytes()).hexdigest()).encode()).hexdigest()[:20]
            values = {"title": current.name + " 검수 결과", "owner": "검수 담당",
                      "due_date": datetime.now(KST).date().isoformat(), "status": "완료",
                      "completion_note": "설정된 정적 검사 통과. Excel 수식 재계산은 미수행."}
            field_names = {field["name"] for field in app["schema"]["fields"]}
            overrides = inputs.get("record_values") or {}
            if not isinstance(overrides, dict):
                raise ValueError("추가 등록값은 항목별 입력으로 전달하세요.")
            values.update({key: value for key, value in overrides.items() if key in field_names})
            try:
                self.validate_record(app, values)
            except ValueError as exc:
                result["checkpoint"] = "registration"
                result["registration"] = {"app_id": app_id, "fields": app["schema"]["fields"], "values": values, "error": str(exc)}
                result["summary"] = "Excel 검사는 끝났습니다. 업무 앱에 등록할 추가 항목을 입력하세요: " + str(exc)
                self.save_result(task_id, result, folder, "waiting_input")
                return
            with LOCK:
                if not self.store.get("records", record_id):
                    self.store.put("records", {"id": record_id, "app_id": app_id, **values, "created_at": now()})
            result["record_id"] = record_id
            self.store.event(task_id, "2/2 검수 결과를 업무 앱에 저장했습니다.")
        else:
            receipt = {"workflow_id": workflow["id"], "task_id": task_id, "source_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
                       "completed_at": now(), "result": "정적 검사 통과", "recalculated": False}
            (folder / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            result.setdefault("artifacts", []).append({"name": "receipt.json", "kind": "json"})
            self.store.event(task_id, "2/2 검수 접수 증빙을 저장했습니다.")
        result["checkpoint"] = "completed"
        result["summary"] = "검수와 결과 등록을 완료했습니다. 검사한 범위와 재계산 여부를 보고서에서 확인하세요."
        with LOCK:
            if self.store.get("workflows", workflow["id"]):
                self.store.update("workflows", workflow["id"], last_run_at=now())
        self.save_result(task_id, result, folder)

    def resume(self, task_id, action, new_input):
        with LOCK:
            task = self.store.get("tasks", task_id)
            if not task:
                raise ValueError("작업을 찾을 수 없습니다.")
            if not task["input"].get("manual") and task["status"] != "waiting_input" and not (action == "repair" and task["agent"] == "excel" and task["status"] == "completed"):
                raise ValueError("확인 대기 중인 작업만 재개할 수 있습니다.")
            if task["input"].get("manual"):
                if action != "continue" or task["status"] not in ("todo", "in_progress", "waiting_input"):
                    raise ValueError("수동 업무는 완료 확인으로 처리하세요.")
                self.store.update("tasks", task_id, status="completed", summary="사용자가 완료를 확인했습니다.", updated_at=now(), completed_at=now())
                self.store.event(task_id, "수동 완료 확인")
                return self.store.get("tasks", task_id)
            inputs = {**task["input"], **new_input}
            if action == "repair":
                if inputs.get("currents"):
                    raise ValueError("일괄 검사 결과는 파일별로 별도 검사를 열어 복원하세요.")
                from .excel_agent import repair_workbook
                folder = self.root / "data" / "artifacts" / task_id / uid("repair")
                repaired = repair_workbook(self.resolve_input(inputs.get("baseline", "baseline")),
                                            self.resolve_input(inputs.get("current", "changed")), folder)
                filename = repaired.get("filename")
                if not filename or not (folder / filename).is_file() or not repaired.get("applied_count"):
                    raise ValueError("자동 복원할 수 있는 수식이 없습니다. 보완 파일을 선택해 재개하세요.")
                upload_id = uid("upload")
                upload_path = self.root / "data" / "uploads"
                upload_path.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copyfile(folder / filename, upload_path / (upload_id + ".xlsx"))
                self.store.put("uploads", {"id": upload_id, "name": filename, "filename": upload_id + ".xlsx",
                                           "size": (folder / filename).stat().st_size, "created_at": now()})
                additions = self.artifacts(task_id, folder, repaired.get("artifacts", []))
                self.store.update("tasks", task_id, artifacts=[*task["artifacts"], *additions])
                inputs["current"] = upload_id
                self.store.event(task_id, "기준 수식의 명확한 복원만 사본에 적용했습니다. 데이터 오류는 다시 확인합니다.")
            self.store.update("tasks", task_id, input=inputs, status="queued", updated_at=now())
            self.store.event(task_id, "보완 입력을 반영하여 같은 작업을 재개합니다.")
            self.pool.submit(self.run, task_id)
            return self.store.get("tasks", task_id)

    def workflow(self, name, prompt="", app_id=None):
        if app_id and self.store.get("apps", app_id) is None:
            raise ValueError("대상 앱을 찾을 수 없습니다.")
        return self.store.put("workflows", {"id": uid("workflow"), "name": name, "description": prompt or "Excel 검사 후 검수 결과를 저장합니다.",
            "app_id": app_id, "steps": [{"tool": "validate_excel", "label": "Excel 품질 검사"}, {"tool": "register_result", "label": "결과 등록"}],
            "enabled": True, "last_run_at": None, "created_at": now(), "trigger": "manual"})

    def check_notifications(self):
        count = 0
        current = datetime.now(KST)
        for task in self.store.all("tasks"):
            if task["status"] in ("completed", "failed") or task.get("archived") or not task.get("due_at"):
                continue
            due = datetime.fromisoformat(task["due_at"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=KST)
            if due <= current + timedelta(hours=2):
                phase = "overdue" if due <= current else "upcoming"
                count += self.store.notify(task["id"] + ":" + phase + ":" + task["due_at"],
                                          "기한이 되었습니다" if phase == "overdue" else "마감 예정",
                                          f"{task['title']} · 기한 {due.strftime('%m/%d %H:%M')} · 현재 {task['status']}", task["id"])
        return count

    def check_due_workflows(self):
        count = 0
        with LOCK:
            current = datetime.now(KST)
            for workflow in self.store.all("workflows"):
                if not workflow.get("enabled") or workflow.get("trigger") != "interval" or not workflow.get("next_run_at"):
                    continue
                due = datetime.fromisoformat(workflow["next_run_at"])
                if due.tzinfo is None:
                    due = due.replace(tzinfo=KST)
                if due > current:
                    continue
                interval = max(1, int(workflow.get("interval_minutes") or 60))
                self.store.update("workflows", workflow["id"],
                                  next_run_at=(current + timedelta(minutes=interval)).isoformat(timespec="seconds"))
                if any(t["status"] in ("queued", "running", "waiting_input") and t["input"].get("workflow_id") == workflow["id"] for t in self.store.all("tasks")):
                    continue
                self.submit("automation", workflow["name"], {"workflow_id": workflow["id"],
                    "baseline": workflow.get("baseline", "baseline"), "current": workflow.get("current", "clean"),
                    "app_id": workflow.get("app_id"), "triggered_by": "schedule"})
                count += 1
        return count

    def brief(self):
        tasks = sorted((t for t in self.store.all("tasks") if t["status"] not in ("completed",) and not t.get("archived")),
                       key=lambda t: (t.get("due_at") or "9999", t["created_at"]))
        memory = self.store.all("memories")
        lines = ["실제 작업 기록을 기준으로 정리했습니다."]
        if not tasks:
            lines.append("현재 진행 중인 업무가 없습니다. Excel 검수나 업무 앱 제작을 맡겨 주세요.")
        labels = {"todo": "할 일", "in_progress": "진행 중", "waiting_input": "확인 필요", "failed": "실패", "running": "실행 중", "queued": "대기"}
        for task in tasks[:8]:
            lines.append(f"• {task['title']} — {labels.get(task['status'], task['status'])}" + (f" / {task['due_at']}" if task.get("due_at") else "") + f" [{task['id']}]")
        if memory:
            lines.append("기억한 업무 맥락: " + " / ".join(f"{m['key']}: {m['value']}" for m in memory[:3]))
        return "\n".join(lines)

    @staticmethod
    def validate_record(app, values):
        for field in app["schema"]["fields"]:
            value = values.get(field["name"])
            kind = field.get("type", "text")
            if kind == "checkbox":
                if value not in (None, "") and not isinstance(value, bool):
                    raise ValueError(f"{field['label']}의 체크 여부를 확인하세요.")
                if field.get("required") and value is not True:
                    raise ValueError(f"{field['label']} 항목을 체크하세요.")
                values[field["name"]] = value is True
                continue
            if field.get("required") and (value is None or not str(value).strip()):
                raise ValueError(f"{field['label']} 항목을 입력하세요.")
            if value in (None, ""):
                continue
            if kind == "number":
                if isinstance(value, bool):
                    raise ValueError(f"{field['label']} 항목에 숫자를 입력하세요.")
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{field['label']} 항목에 숫자를 입력하세요.")
                if not math.isfinite(numeric):
                    raise ValueError(f"{field['label']} 항목에 유한한 숫자를 입력하세요.")
                if field.get("min") is not None and numeric < field["min"]:
                    raise ValueError(f"{field['label']} 항목은 {field['min']} 이상이어야 합니다.")
                if field.get("max") is not None and numeric > field["max"]:
                    raise ValueError(f"{field['label']} 항목은 {field['max']} 이하여야 합니다.")
                values[field["name"]] = numeric
                continue
            if not isinstance(value, str):
                raise ValueError(f"{field['label']} 항목은 문자열이어야 합니다.")
            if value is not None and len(str(value)) > 4000:
                raise ValueError("입력값이 너무 깁니다.")
            if field.get("options") and value not in (None, "") and value not in field["options"]:
                raise ValueError(f"{field['label']}의 허용값을 확인하세요.")
            if field.get("type") == "date" and value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except (ValueError, TypeError):
                    raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
            if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
                raise ValueError(f"{field['label']} 항목에 올바른 이메일을 입력하세요.")
        if app["schema"].get("require_completion_note") and values.get("status") == "완료" and not str(values.get("completion_note", "")).strip():
            raise ValueError("완료 처리 시 처리 내용을 입력하세요.")
        if app["schema"].get("prevent_past_due_date") and values.get("due_date") and values["due_date"] < datetime.now(KST).date().isoformat():
            raise ValueError("마감일은 오늘 이후여야 합니다.")
