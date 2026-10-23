import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.excel_agent import inspect_workbook, inspect_workbooks
from backend.orchestrator import Engine
from backend.showcase_seed import LABEL, SOURCE_FILENAMES, UPLOAD_IDS, PREVIOUS_UPLOAD_IDS, PREVIOUS_FORMAT_UPLOAD_IDS, seed_showcase
from backend.store import Store


@pytest.fixture
def workspace(tmp_path):
    project = Path(__file__).resolve().parents[1]
    sources = tmp_path / "data" / "examples" / "excel_showcase"
    sources.mkdir(parents=True)
    for name in SOURCE_FILENAMES.values():
        source = project / "data" / "examples" / "excel_showcase" / name
        if not source.exists():
            source = project.parent / name
        (sources / name).write_bytes(source.read_bytes())
    return tmp_path, Store(tmp_path / "data" / "isolated-test.db")


def test_registers_real_inspectable_files_and_two_examples(workspace):
    root, store = workspace
    result = seed_showcase(root, store)
    assert result["created_counts"] == {"uploads": 2, "examples": 2}
    assert len(result["uploads"]) == len(result["examples"]) == 2
    assert all(item["synthetic"] and item["sourceLabel"] == LABEL for group in ("uploads", "examples") for item in result[group])
    engine = Engine(root, store, planner=None)
    try:
        single, batch = result["examples"]
        inputs = single["input"]
        inspected = inspect_workbook(engine.resolve_input(inputs["baseline"]), engine.resolve_input(inputs["current"]), root / "single")
        assert inspected["counts"] == {"errors": 26, "warnings": 23, "changes": 45}
        assert inspected["repairable_count"] == 5
        batch_inputs = batch["input"]
        compared = inspect_workbooks(engine.resolve_input(batch_inputs["baseline"]), [engine.resolve_input(value) for value in batch_inputs["currents"]], root / "batch")
        assert compared["batch_counts"]["total_files"] == 2
        assert compared["batch_counts"]["completed_files"] == 2
        for role, name in SOURCE_FILENAMES.items():
            assert engine.resolve_input(UPLOAD_IDS[role]).read_bytes() == (root / "data" / "examples" / "excel_showcase" / name).read_bytes()
    finally:
        engine.stop()
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(root) not in serialized
    assert not any(word in serialized for word in ("합성", "데모", "시연"))
    assert "source_path" not in serialized and "filename" not in serialized


def test_repeated_seed_preserves_metadata_files_and_user_records(workspace):
    root, store = workspace
    first = seed_showcase(root, store)
    upload_id = UPLOAD_IDS["current"]
    upload = store.update("uploads", upload_id, name="담당자가 바꾼 제목.xlsx", source_path="private-internal-path")
    example_id = first["examples"][0]["id"]
    example = store.update("examples", example_id, title="수정한 업무 예제", source_path="private-example-path")
    target = root / "data" / "uploads" / upload["filename"]
    target.write_bytes(b"user-modified-file")
    user = store.put("uploads", {"id": "user_upload", "name": "사용자 자료"})
    repeated = seed_showcase(root, store)
    assert repeated["created_counts"] == {"uploads": 0, "examples": 0}
    assert store.get("uploads", upload_id) == upload
    assert store.get("examples", example_id) == example
    assert store.get("uploads", "user_upload") == user
    assert target.read_bytes() == b"user-modified-file"
    assert "private-" not in json.dumps(repeated)


def test_missing_source_and_unregistered_file_are_not_overwritten(workspace):
    root, store = workspace
    source = root / "data" / "examples" / "excel_showcase" / SOURCE_FILENAMES["current"]
    source.unlink()
    target = root / "data" / "uploads" / (UPLOAD_IDS["baseline"] + ".xlsx")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"preexisting-unregistered-content")
    result = seed_showcase(root, store)
    assert result["created_total"] == 0
    assert not result["uploads"] and not result["examples"]
    assert len(result["warnings"]) == 2
    assert target.read_bytes() == b"preexisting-unregistered-content"
    assert str(root) not in json.dumps(result)


def test_concurrent_calls_insert_one_set(workspace):
    root, store = workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: seed_showcase(root, store), range(2)))
    assert sum(item["created_total"] for item in outcomes) == 4
    assert len(store.all("uploads")) == 2
    assert len(store.all("examples")) == 2


@pytest.mark.parametrize("previous_ids", [PREVIOUS_UPLOAD_IDS, PREVIOUS_FORMAT_UPLOAD_IDS])
def test_format_update_only_relinks_original_examples_and_preserves_old_uploads(workspace, previous_ids):
    root, store = workspace
    old_upload = {"id": previous_ids["baseline"], "name": "이전 파일", "filename": "original.xlsx", "synthetic": True}
    store.put("uploads", old_upload)
    example_id = "demo_v3_example_excel_showcase_single"
    store.put("examples", {"id": example_id, "title": "사용자가 바꾼 제목", "synthetic": True,
                            "input": {"baseline": previous_ids["baseline"], "current": previous_ids["current"]}})
    result = seed_showcase(root, store)
    assert result["created_counts"] == {"uploads": 2, "examples": 1}
    assert store.get("uploads", old_upload["id"]) == old_upload
    assert store.get("examples", example_id)["title"] == "사용자가 바꾼 제목"
    assert store.get("examples", example_id)["input"] == {"baseline": UPLOAD_IDS["baseline"], "current": UPLOAD_IDS["current"]}
