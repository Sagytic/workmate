"""Idempotent synthetic workspace data; no model calls or fabricated tool runs."""
from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .builder import create_app
from .store import KST, LOCK, now

PREFIX = "demo_v3_"
LABEL = "업무 예제"
MARK = {"synthetic": True, "sourceLabel": LABEL}
ASSET_DIR = Path(__file__).resolve().parent.parent / "data" / "examples"
COLLECTIONS = ("projects", "tasks", "documents", "memories", "notifications", "apps", "records", "workflows", "examples")

PROJECTS = [
    ("erp", "ERP 전환", "#64748b", "기준정보·잔액 대조·인터페이스와 인수 준비를 다루는 ERP 전환 프로젝트."),
    ("finance", "금융 정산", "#657b70", "접수 마감, 금액 차이와 재처리 절차를 검토하는 정산 운영 프로젝트."),
    ("portal", "고객 포털", "#7d718b", "고객 요청 접수와 필수 입력 변경을 검토하는 업무 포털 프로젝트."),
    ("backoffice", "백오피스", "#998166", "견적·증빙·월말 마감 자료를 관리하는 실무 업무 묶음."),
    ("common", "공통 업무", "#6b8191", "주간 공유, 업무 인계와 지식 정리를 위한 공통 업무."),
]
OWNERS = ("서윤 PM", "민재 개발", "지수 QA", "현우 운영", "다은 업무")
TASK_TITLES = {
    "erp": ["전환 기준정보 코드 매핑 차이 확인", "잔액 대조 예외 3건 업무 확인 대기", "인터페이스 필수 항목 누락 기준 정리", "전환 리허설 입력 파일 준비", "예외 처리 재개 절차 검토", "고객 인수 범위 확정 요청 준비", "기준정보 표 1차 검토", "전환 산출물 목록 정리"],
    "finance": ["정산 예정액과 실제액 차이 원인 분류", "16시 30분 마감 변경 승인 검토 대기", "정산 가이드 v1·v2 영향 비교", "재접수 차수와 중복 키 확인", "금액 검토자·승인자 역할표 작성", "정산 보류 사유 보완 답변 대기", "거래 건수 표 대조 메모 정리", "운영 인계 항목 초안 정리"],
    "portal": ["완료 처리 근거 누락 항목 분류", "긴급 요청 분류 권한 결정 대기", "담당자·마감일 필수화 UAT 설계", "고객 요청 목록 필터 경계값 검토", "요구사항 변경 영향 담당자 배정", "기존 완료 건 보완 기한 결정 대기", "요구사항 v1 접수 항목 정리", "고객 협의 미결정 목록 정리"],
    "backoffice": ["견적 수정본 합계와 제외 범위 비교", "공급 범위 누락 항목 확인 대기", "월말 증빙 필수 항목 체크리스트 작성", "견적 공수와 투입 기간 중복 검토", "납기·유지보수 조건 비교표 준비", "부가세 포함 여부 답변 대기", "견적 접수 차수 목록 정리", "증빙 자료 분류 기준 초안 정리"],
    "common": ["주간 보고 미해결 항목 담당자 확인", "부문 공통 인수 양식 의견 대기", "자료 근거를 붙인 주간 보고 작성", "보안 FAQ와 외부 전송 범위 검토", "새 담당자 업무 인계 문서 준비", "다음 기능 검토 의견 취합 대기", "업무 자료 분류 규칙 정리", "공통 업무 우선순위 초안 정리"],
}
DOCUMENTS = [
    ("requirements_v1", "고객 포털 요구사항 v1.0", "요구사항", "고객 포털", "demo_v3_requirements_v1.txt"),
    ("requirements_v2", "고객 포털 요구사항 v2.0 변경 검토안", "요구사항", "고객 포털", "demo_v3_requirements_v2.txt"),
    ("finance_v1", "금융 정산 운영 가이드 v1.0", "운영 가이드", "금융 정산", "demo_v3_finance_guide_v1.txt"),
    ("finance_v2", "금융 정산 운영 가이드 v2.0 변경 검토안", "운영 가이드", "금융 정산", "demo_v3_finance_guide_v2.txt"),
    ("portal_meeting", "고객 포털 협의 안건과 결정 메모", "회의·결정", "고객 포털", "demo_v3_portal_meeting.txt"),
    ("quote_review", "SI 견적 검토 기준과 확인 질문", "업무 기준", "백오피스", "demo_v3_quote_review.txt"),
    ("incident_retro", "정산 인터페이스 지연 장애 회고", "장애 회고", "금융 정산", "demo_v3_incident_retro.txt"),
    ("security_faq", "AI 업무 도구 사용 범위 FAQ", "FAQ", "공통 업무", "demo_v3_security_faq.txt"),
    ("acceptance", "업무 앱 인수 검수 기준", "인수 기준", "ERP 전환", "demo_v3_acceptance.txt"),
    ("weekly_notes", "ERP·금융 SI 부문 주간 업무 메모", "주간 메모", "공통 업무", "demo_v3_weekly_notes.txt"),
]


def _field(name, label, kind="text", required=False, **extra):
    return {"name": name, "label": label, "type": kind, "required": required, **extra}


def _app_specs():
    return [
        {"key": "requests", "name": "고객 요청 접수", "project": "고객 포털",
         "description": "고객 요청의 접수, 담당자, 예상 공수와 처리 상태를 관리합니다.",
         "fields": [_field("module", "업무 모듈", "select", True, options=["인증", "접수", "대시보드", "공통"]),
                    _field("request_type", "요청 구분", "select", True, options=["개선", "오류", "문의"]),
                    _field("effort_hours", "예상 공수(시간)", "number", False, min=0, max=160),
                    _field("customer_email", "회신 참고 이메일", "email")],
         "titles": ["담당자 필수 입력 요청", "완료 메모 누락 문의", "마감 임박 목록 기준 확인", "요청 구분 선택지 검토", "CSV 한글 표시 확인", "제목 키워드 검색 개선", "진행 상태 수정 흐름 확인", "중복 접수 안내 문구 검토", "고객 이메일 형식 확인", "공수 0시간 입력 사례", "기존 완료 건 수정 기준", "담당자별 목록 필터 요청"]},
        {"key": "settlements", "name": "정산 차이 검토", "project": "금융 정산",
         "description": "정산 건수·예정액·실제액과 검토 상태를 저장하고 차이 발생 건을 확인합니다.",
         "fields": [_field("settlement_date", "정산 기준일", "date", True),
                    _field("transaction_count", "거래 건수", "number", True, min=0, max=10000),
                    _field("expected_amount", "정산 예정액", "number", True, min=0, max=1000000000),
                    _field("actual_amount", "정산 실제액", "number", True, min=0, max=1000000000),
                    _field("review_state", "대조 구분", "select", True, options=["차이 없음", "차이 확인", "검토 보류"])],
         "require_completion_note": True,
         "titles": ["채널 A 일일 정산", "채널 B 수수료 차이", "취소 거래 반영 검토", "재접수 2차 정산", "거래 묶음 대조", "정산 마감 직전 접수", "환불 처리 시점 확인", "영업일 경계 자료 확인", "검토자 인계 사례", "보류 사유 보완 사례", "전일 미처리 건 대조", "예정액 일치 사례"]},
        {"key": "quotes", "name": "견적·증빙 검토", "project": "백오피스",
         "description": "공급사 견적의 원가·제안가·검토 단계와 증빙 확인 내용을 관리합니다.",
         "fields": [_field("supplier", "공급사", "text", True),
                    _field("cost_amount", "원가(원)", "number", True, min=0, max=1000000000),
                    _field("offer_amount", "제안가(원)", "number", True, min=0, max=1000000000),
                    _field("quote_stage", "검토 단계", "select", True, options=["초안", "내부 검토", "질문 정리"]),
                    _field("vat_included", "부가세 포함", "checkbox")],
         "titles": ["ERP 인터페이스 보완 견적", "테스트 지원 공수 견적", "데이터 이행 작업 견적", "운영 인계 지원 견적", "고객 포털 개선 견적", "정산 예외 처리 견적", "문서 현행화 작업 견적", "접수 화면 개선 견적", "품질 검토 지원 견적", "모니터링 화면 견적", "백오피스 양식 정비 견적", "검수 시나리오 보완 견적"]},
        {"key": "risks", "name": "프로젝트 이슈·위험", "project": "공통 업무",
         "description": "프로젝트의 일정·품질·환경·데이터 위험과 확인·대응 메모를 관리합니다.",
         "fields": [_field("project_name", "프로젝트", "select", True, options=[p[1] for p in PROJECTS]),
                    _field("severity", "영향 수준", "select", True, options=["낮음", "보통", "높음"]),
                    _field("risk_type", "위험 구분", "select", True, options=["일정", "품질", "환경", "데이터"]),
                    _field("mitigation", "확인·대응 메모", "textarea"),
                    _field("acknowledged", "검토 대상 확인", "checkbox", True)],
         "titles": ["기준정보 보완 일정 지연", "인수 범위 결정 대기", "정산 재처리 키 불일치", "필수 입력 규칙 누락", "테스트 환경 준비 지연", "원천·수정본 혼용 위험", "검토 역할 공백", "완료 근거 누락", "견적 제외 범위 불명확", "필수 증빙 접수 지연", "운영 인계 정보 부족", "변경 문서 버전 혼동"]},
    ]


def _stamp(value):
    return value.astimezone(KST).isoformat(timespec="seconds")


def _record_values(spec, index, anchor):
    key = spec["key"]
    status = ("대기", "진행", "완료")[index % 3]
    values = {"title": spec["titles"][index], "owner": OWNERS[index % len(OWNERS)],
              "due_date": (anchor + timedelta(days=(index % 10) - 2)).date().isoformat(),
              "status": status, "completion_note": "완료 상태로 등록된 업무입니다. 상세 처리 내용은 관련 자료에서 확인하세요." if status == "완료" else ""}
    if key == "requests":
        values.update(module=["인증", "접수", "대시보드", "공통"][index % 4],
                      request_type=["개선", "오류", "문의"][index % 3], effort_hours=[2, 4, 8, 0, 16, 24][index % 6],
                      customer_email=f"customer-{index + 1:02}@example.test")
    elif key == "settlements":
        expected = 1500000 + index * 275000
        delta = [0, -1500, 0, 2000, 0, -3000][index % 6]
        values.update(settlement_date=(anchor - timedelta(days=1 + index % 6)).date().isoformat(),
                      transaction_count=120 + index * 17, expected_amount=expected, actual_amount=expected + delta,
                      review_state="차이 없음" if not delta else "검토 보류" if index % 3 == 1 else "차이 확인")
    elif key == "quotes":
        cost = 1800000 + index * 425000
        values.update(supplier=f"공급사 {chr(65 + index % 4)}", cost_amount=cost,
                      offer_amount=cost + 350000 + index * 25000,
                      quote_stage=["초안", "내부 검토", "질문 정리"][index % 3], vat_included=index % 2 == 0)
    elif key == "risks":
        values.update(project_name=PROJECTS[index % len(PROJECTS)][1], severity=["높음", "보통", "낮음"][index % 3],
                      risk_type=["일정", "품질", "환경", "데이터"][index % 4],
                      mitigation="근거 문서와 담당 역할을 확인하고 미확정 항목은 결정 대기로 남깁니다.", acknowledged=True)
    return values


def seed_demo(root: Path, store) -> dict:
    """Add missing demo objects only; retain edits, user data and prior tool outcomes."""
    from .orchestrator import Engine

    root = Path(root).resolve()
    created = {collection: 0 for collection in COLLECTIONS}
    warnings = []
    with LOCK:
        existing_project = store.get("projects", PREFIX + "project_erp")
        saved_anchor = existing_project.get("seed_anchor_at") if existing_project and existing_project.get("synthetic") else None
        anchor = datetime.fromisoformat(saved_anchor or now()).astimezone(KST)
        timestamp = _stamp(anchor)

        def add(collection, obj):
            if store.get(collection, obj["id"]) is not None:
                return False
            store.put(collection, {**obj, **MARK})
            created[collection] += 1
            return True

        for project_index, (key, name, color, description) in enumerate(PROJECTS):
            add("projects", {"id": PREFIX + "project_" + key, "name": name, "color": color,
                             "description": description, "created_at": timestamp, "seed_anchor_at": timestamp})
            statuses = ("in_progress", "waiting_input", "todo", "in_progress", "todo", "waiting_input", "completed", "completed")
            offsets = (-2, 0, 1, 2 + project_index, 5 + project_index, 8 + project_index, -1, -2)
            for index, title in enumerate(TASK_TITLES[key]):
                status = statuses[index]
                task_id = f"{PREFIX}task_{key}_{index + 1:02}"
                due = (anchor + timedelta(days=offsets[index])).replace(hour=17 if index % 2 else 11, minute=0, second=0, microsecond=0)
                completed = anchor - timedelta(days=1 + (project_index + index) % 6) if status == "completed" else None
                summary = (f"{title} · 완료 상태" if completed else
                           "담당자의 확인 또는 추가 입력을 기다리고 있습니다." if status == "waiting_input" else
                           "담당자가 업무를 진행 중입니다." if status == "in_progress" else
                           "담당 업무와 일정을 확인한 뒤 시작할 예정입니다.")
                event = [{"at": _stamp(completed), "message": f"업무 상태: 완료 · {title}", **MARK}] if completed else []
                item = {"id": task_id, "agent": "assistant", "title": title, "status": status,
                        "project": name, "project_id": PREFIX + "project_" + key,
                        "owner": OWNERS[(project_index + index) % len(OWNERS)],
                        "tags": [name, "업무 예제", "검토" if index % 2 else "실무"],
                        "priority": "high" if index in (0, 1) else "normal" if index < 6 else "low",
                        "created_at": _stamp(anchor - timedelta(days=14 + index)),
                        "updated_at": _stamp(completed or anchor - timedelta(hours=index + project_index)),
                        "due_at": _stamp(due), "completed_at": _stamp(completed) if completed else None,
                        "summary": summary, "archived": False, "mode": "demo", "attempt": 0,
                        "input": {"manual": True, "message": title, "project": name, **MARK},
                        "result": {}, "events": event, "artifacts": []}
                add("tasks", item)

        example_dir = root / "data" / "examples"
        example_dir.mkdir(parents=True, exist_ok=True)
        document_files = []
        for index, (key, title, category, project, filename) in enumerate(DOCUMENTS):
            source = ASSET_DIR / filename
            target = example_dir / filename
            body = source.read_text(encoding="utf-8")
            if source.resolve() != target.resolve() and not target.exists():
                target.write_text(body, encoding="utf-8", newline="\n")
            doc_id = PREFIX + "doc_" + key
            add("documents", {"id": doc_id, "title": title, "body": body, "category": category,
                              "project": project, "created_at": _stamp(anchor - timedelta(days=12 - index)),
                              "updated_at": _stamp(anchor - timedelta(days=1 if key.endswith("v2") else 3)),
                              "filename": filename, "source_path": str(target), "paragraph_count": len([p for p in body.split("\n\n") if p.strip()])})
            document_files.append({"id": doc_id, "document_id": doc_id, "filename": filename, "path": str(target), "title": title, **MARK})

        memories = [
            ("report_format", "주간 보고 구성", "기간 내 완료 / 진행 중 / 막힌 항목 / 다음 계획 / 필요한 결정을 나누고 관련 작업 ID 또는 문서 문단을 붙입니다."),
            ("team_scope", "SI 부문 업무 범위", "ERP 전환, 금융 정산, 고객 포털, 백오피스와 공통 업무를 프로젝트별로 관리합니다."),
            ("excel_review", "Excel 검수 원칙", "기준과 수정본을 구분하고 유일한 ID로 행을 비교합니다. 정적 검사와 재계산을 구분하며 원본은 유지합니다."),
            ("uat_evidence", "UAT 결과 표기", "실제 절차·기대 결과·관찰 결과·화면 근거를 연결합니다. 미실행 또는 지원하지 않는 범위를 통과로 기록하지 않습니다."),
            ("priority", "업무 우선순위", "마감이 지난 진행 업무, 담당자의 결정이 필요한 항목, 오늘 마감 순으로 확인합니다. 근거 없는 진척률은 만들지 않습니다."),
        ]
        for key, title, value in memories:
            add("memories", {"id": PREFIX + "memory_" + key, "key": title, "value": value, "source": LABEL, "updated_at": timestamp})

        for spec in _app_specs():
            app_id = PREFIX + "app_" + spec["key"]
            folder = root / "data" / "artifacts" / (PREFIX + "apps") / spec["key"]
            business_app = store.get("apps", app_id)
            if business_app is None:
                if folder.exists() and any(folder.iterdir()):
                    warnings.append(f"{app_id}: 기존 파일을 덮어쓰지 않기 위해 앱 생성을 건너뛰었습니다.")
                    continue
                plan = {"name": spec["name"], "description": spec["description"], "fields": copy.deepcopy(spec["fields"]),
                        "require_completion_note": spec.get("require_completion_note", False)}
                generated = create_app(spec["description"], app_id, folder, plan=plan)
                add("apps", {"id": app_id, "name": generated["name"], "description": generated["description"],
                             "project": spec["project"], "schema": generated["schema"],
                             "source_path": str(folder / generated["source_filename"]), "source_sha256": generated["source_sha256"],
                             "created_at": timestamp, "version": 1, "url": f"/api/apps/{app_id}/view",
                             "generation": "allowlisted-schema-template", "verification_status": "not_tested"})
                business_app = store.get("apps", app_id)
            if not business_app.get("synthetic") or business_app.get("sourceLabel") != LABEL:
                warnings.append(f"{app_id}: 기존 사용자 앱에 예제 레코드를 추가하지 않았습니다.")
                continue
            if not Path(business_app.get("source_path", "")).is_file():
                warnings.append(f"{app_id}: 저장된 앱 소스가 없습니다. 기존 메타데이터는 유지했습니다.")
            for index in range(12):
                record_id = f"{PREFIX}record_{spec['key']}_{index + 1:02}"
                if store.get("records", record_id) is not None:
                    continue
                values = _record_values(spec, index, anchor)
                allowed = {field["name"] for field in business_app["schema"]["fields"]}
                values = {key: value for key, value in values.items() if key in allowed}
                try:
                    Engine.validate_record(business_app, values)
                except ValueError as exc:
                    warnings.append(f"{record_id}: 변경된 앱의 입력 규칙을 존중하여 생성을 건너뛰었습니다. {exc}")
                    continue
                add("records", {"id": record_id, "app_id": app_id, **values, "project": spec["project"],
                                "created_at": _stamp(anchor - timedelta(days=12 - index)), "updated_at": timestamp})

        workflow_specs = [
            ("review", "요청 파일 검수 후 접수", "manual", "changed", PREFIX + "app_requests", 60),
            ("daily", "정기 산출물 검수", "interval", "clean", None, 60),
            ("finance", "정산 파일 제출 전 확인", "manual", "reordered", None, 60),
        ]
        for key, name, trigger, current, app_id, minutes in workflow_specs:
            add("workflows", {"id": PREFIX + "workflow_" + key, "name": name,
                              "description": "Excel 검사 → 보완 대기 → 결과 등록 절차입니다. 자동 실행은 기본적으로 꺼져 있습니다.",
                              "steps": [{"tool": "validate_excel", "label": "Excel 품질 검사"}, {"tool": "register_result", "label": "결과 등록"}],
                              "enabled": False, "trigger": trigger, "interval_minutes": minutes,
                              "baseline": "baseline", "current": current, "app_id": app_id,
                              "next_run_at": None, "last_run_at": None, "created_at": timestamp})

        for index, (project_key, task_index, title) in enumerate([
            ("erp", 1, "ERP 대조 업무의 기한 확인"), ("finance", 2, "정산 마감 변경 결정 대기"),
            ("portal", 2, "긴급 요청 분류 권한 확인"), ("backoffice", 1, "견적 수정본 검토"),
            ("common", 3, "주간 보고 작성 준비"), ("portal", 8, "완료 상태 업무 기록 안내"),
        ], 1):
            add("notifications", {"id": f"{PREFIX}notification_{index:02}", "title": title,
                                  "message": "연결된 업무의 상태와 기한을 확인하고 필요한 후속 조치를 정리하세요.",
                                  "task_id": f"{PREFIX}task_{project_key}_{task_index:02}", "read": index > 4,
                                  "created_at": _stamp(anchor - timedelta(minutes=15 * index)), "kind": "synthetic_demo"})

        catalog = json.loads((ASSET_DIR / "demo_v3_catalog.json").read_text(encoding="utf-8"))
        for example in catalog["examples"]:
            add("examples", {**example, "created_at": timestamp})
        for filename in ("demo_v3_catalog.json", "demo_v3_erp_cutover.txt", "README.md"):
            source, target = ASSET_DIR / filename, example_dir / filename
            if source.is_file() and source.resolve() != target.resolve() and not target.exists():
                target.write_bytes(source.read_bytes())
        totals = {collection: sum(obj.get("synthetic") is True and obj["id"].startswith(PREFIX) for obj in store.all(collection)) for collection in COLLECTIONS}
        from .tickets import seed_tickets
        seed_tickets(store)
        tasks = [task for task in store.all("tasks") if task["id"].startswith(PREFIX) and task.get("synthetic")]
        return {"created_counts": created, "created_total": sum(created.values()), "totals": totals,
                "task_status_counts": dict(Counter(task["status"] for task in tasks)),
                "examples": [store.get("examples", example["id"]) for example in catalog["examples"]],
                "document_files": document_files, "seed_anchor_at": timestamp, "warnings": warnings,
                "note": "업무 예제를 준비했습니다. 기존 항목과 사용자 변경은 유지했으며 앱의 검수 결과는 실행 후 확인할 수 있습니다.", **MARK}
