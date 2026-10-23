"""Register the bundled Excel examples without replacing existing records or files."""
from __future__ import annotations

from pathlib import Path

from .store import LOCK, now


SOURCE_FILENAMES = {
    "baseline": "Excel_검증_기준본.xlsx",
    "current": "Excel_검증_수정본.xlsx",
}
UPLOAD_IDS = {role: f"demo_v3_excel_showcase_fmt3_{role}" for role in SOURCE_FILENAMES}
PREVIOUS_UPLOAD_IDS = {role: f"demo_v3_excel_showcase_{role}" for role in SOURCE_FILENAMES}
PREVIOUS_FORMAT_UPLOAD_IDS = {role: f"demo_v3_excel_showcase_fmt2_{role}" for role in SOURCE_FILENAMES}
LABEL = "업무 예제"
MARK = {"synthetic": True, "sourceLabel": LABEL}
UPLOAD_PUBLIC_FIELDS = ("id", "name", "size", "created_at", "synthetic", "sourceLabel")
EXAMPLE_PUBLIC_FIELDS = (
    "id", "title", "category", "description", "agent", "expected", "estimated_seconds",
    "project", "difficulty", "uses_llm", "created_at", "synthetic", "sourceLabel",
)


def _examples(timestamp):
    baseline, current = UPLOAD_IDS["baseline"], UPLOAD_IDS["current"]
    common = {
        "category": "Excel 검증", "agent": "excel", "project": "ERP 전환",
        "difficulty": "종합", "uses_llm": False, "created_at": timestamp, **MARK,
    }
    return [
        {**common, "id": "demo_v3_example_excel_showcase_single", "title": "Excel 59개 사례 종합 검증",
         "description": "14개 시트의 수식·입력값·ID·유효성 규칙·행과 시트 변경을 검사하고 복원 가능한 수식을 구분합니다. 검사 목적으로 오류를 넣은 업무 예제입니다.",
         "input": {"baseline": baseline, "current": current}, "estimated_seconds": 5,
         "expected": "제공 파일 기준 오류 26건·검토 23건·변경 45건과 수식 복원 제안 5개를 확인합니다. 파일을 수정하면 결과가 달라집니다. 재계산은 수행하지 않습니다."},
        {**common, "id": "demo_v3_example_excel_showcase_batch", "title": "기준본·수정본 일괄 비교",
         "description": "같은 기준에 수정본과 기준본을 각각 대조하여 파일별 발견 사항·검사 범위·보고서를 확인합니다. 기준본에도 한계 확인용 중복 ID와 미지원 수식이 있습니다.",
         "input": {"baseline": baseline, "currents": [current, baseline]}, "estimated_seconds": 8,
         "expected": "2개 파일을 독립 검사하고 HTML·CSV 결과를 제공합니다. 기준본 자체의 오류·미검증 항목도 남습니다. 일괄 자동 복원과 재계산은 수행하지 않습니다."},
    ]


def _public_example(item):
    result = {key: item[key] for key in EXAMPLE_PUBLIC_FIELDS if key in item}
    inputs = item.get("input")
    if isinstance(inputs, dict):
        # Existing records can contain application-private keys; never expose them here.
        result["input"] = {key: inputs[key] for key in ("baseline", "current", "currents") if key in inputs}
    return result


def seed_showcase(root: Path, store) -> dict:
    """Insert missing upload/example metadata; preserve all existing user changes.

    Source names are explicit and relative to the supplied application root. No
    directory enumeration, absolute source paths or generated execution results
    are returned. Missing sources leave examples unregistered until a later call.
    """
    root = Path(root).resolve()
    source_dir = root / "data" / "examples" / "excel_showcase"
    upload_dir = root / "data" / "uploads"
    created = {"uploads": 0, "examples": 0}
    warnings, available = [], set()
    timestamp = now()
    definitions = _examples(timestamp)

    with LOCK:
        for role, source_name in SOURCE_FILENAMES.items():
            upload_id = UPLOAD_IDS[role]
            existing = store.get("uploads", upload_id)
            if existing is None:
                source = source_dir / source_name
                if not source.is_file() or not source.resolve().is_relative_to(source_dir.resolve()):
                    warnings.append(f"{source_name}: 예제 원본이 없어 등록하지 않았습니다.")
                    continue
                filename = upload_id + ".xlsx"
                target = upload_dir / filename
                if target.exists():
                    warnings.append(f"{source_name}: 미등록 상태의 기존 파일을 보존하기 위해 등록하지 않았습니다.")
                    continue
                upload_dir.mkdir(parents=True, exist_ok=True)
                # Exclusive creation also protects a file created outside Store's lock.
                content = source.read_bytes()
                try:
                    with target.open("xb") as stream:
                        stream.write(content)
                except FileExistsError:
                    warnings.append(f"{source_name}: 기존 파일을 보존하기 위해 등록하지 않았습니다.")
                    continue
                existing = {"id": upload_id, "name": source_name, "filename": filename,
                            "size": len(content), "created_at": timestamp, **MARK}
                store.put("uploads", existing)
                created["uploads"] += 1

            filename = existing.get("filename", "")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename:
                warnings.append(f"{upload_id}: 기존 파일 연결 정보를 유지했으며 예제 등록에는 사용하지 않았습니다.")
                continue
            target = upload_dir / filename
            if not target.is_file() or not target.resolve().is_relative_to(upload_dir.resolve()):
                warnings.append(f"{upload_id}: 기존 파일 연결은 유지했지만 저장된 파일이 없습니다.")
                continue
            if not existing.get("synthetic"):
                warnings.append(f"{upload_id}: 기존 사용자 업로드를 예제에 연결하지 않았습니다.")
                continue
            available.add(role)

        if len(available) == len(SOURCE_FILENAMES):
            for example in definitions:
                if store.get("examples", example["id"]) is None:
                    store.put("examples", example)
                    created["examples"] += 1
                else:
                    previous = store.get("examples", example["id"])
                    old_inputs = []
                    for previous_ids in (PREVIOUS_UPLOAD_IDS, PREVIOUS_FORMAT_UPLOAD_IDS):
                        old_input = {"baseline": previous_ids["baseline"]}
                        old_input.update({"currents": [previous_ids["current"], previous_ids["baseline"]]} if "currents" in example["input"] else {"current": previous_ids["current"]})
                        old_inputs.append(old_input)
                    if previous.get("synthetic") and previous.get("input") in old_inputs:
                        # Upgrade only the untouched bundled example link. Prior tasks/uploads
                        # and user-edited titles, settings or custom inputs remain unchanged.
                        store.update("examples", example["id"], input=example["input"])

        uploads = [store.get("uploads", upload_id) for upload_id in UPLOAD_IDS.values()]
        examples = [store.get("examples", example["id"]) for example in definitions]
        return {
            "uploads": [{key: item[key] for key in UPLOAD_PUBLIC_FIELDS if key in item} for item in uploads if item],
            "examples": [_public_example(item) for item in examples if item],
            "created_counts": created, "created_total": sum(created.values()), "warnings": warnings,
            "note": "기존 업로드·예제와 수정된 파일은 유지했습니다. 예제 등록은 검사 실행이나 통과를 뜻하지 않습니다.",
            **MARK,
        }
