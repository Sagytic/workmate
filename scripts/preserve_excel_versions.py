"""Preserve prior fixture bytes before a presentation update, via the existing Store."""
from pathlib import Path
import copy
import hashlib
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.store import Store, LOCK, now


def main():
    work = ROOT.parent.parent / "work" / "excel-format-v2"
    originals = work / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    roles = ("baseline", "changed", "clean", "reordered")
    for role in roles:
        target = originals / (role + ".xlsx")
        if not target.exists():
            target.write_bytes((ROOT / "data" / "fixtures" / target.name).read_bytes())
    for filename in ("Excel_검증_기준본.xlsx", "Excel_검증_수정본.xlsx"):
        target = originals / filename
        if not target.exists():
            target.write_bytes((ROOT.parent / filename).read_bytes())
    database = ROOT / "data" / "workmate.db"
    if not database.is_file():
        print(json.dumps({"preserved_files": 6, "database": "absent; no task migration"}))
        return
    store = Store(database)
    with LOCK:
        backup = work / "before-excel-format-v2.db"
        if not backup.exists():
            with store.connect() as source, sqlite3.connect(backup) as destination:
                source.backup(destination)
        tasks = [t for t in store.all("tasks") if t.get("agent") in ("excel", "automation") and t.get("status") not in ("queued", "running")]
        mapping, preserved = {}, []
        upload_dir = ROOT / "data" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        for role in roles:
            content = (originals / (role + ".xlsx")).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            key = f"excel_source_v1_{role}_{digest[:12]}"
            target = upload_dir / (key + ".xlsx")
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(content)
            assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
            if store.get("uploads", key) is None:
                store.put("uploads", {"id": key, "name": f"{role} · 이전 작업 원본.xlsx", "filename": target.name,
                                      "size": len(content), "created_at": now(), "synthetic": True,
                                      "sourceLabel": "업무 예제", "sha256": digest, "format_version": 1})
            mapping[role] = key
            preserved.append({"role": role, "upload_id": key, "sha256": digest})
        def translate(value, key=None):
            if isinstance(value, dict):
                return {k: translate(v, k) for k, v in value.items()}
            if key == "currents" and isinstance(value, list):
                return [mapping.get(v, v) if isinstance(v, str) else v for v in value]
            if key in ("baseline", "current") and isinstance(value, str):
                return mapping.get(value, value)
            return value
        changed = []
        for task in tasks:
            original = task.get("input", {})
            updated = translate(copy.deepcopy(original))
            if updated != original:
                store.update("tasks", task["id"], input=updated)
                changed.append({"task_id": task["id"], "previous_input": original, "input": updated})
        report = {"preserved_files": 6, "uploads": preserved, "updated_task_count": len(changed), "tasks": changed,
                  "running_tasks_not_changed": [t["id"] for t in store.all("tasks") if t.get("agent") in ("excel", "automation") and t.get("status") in ("queued", "running")]}
        (work / "source-preservation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"preserved_files": 6, "snapshot_uploads": len(preserved), "updated_tasks": len(changed), "active_tasks": report["running_tasks_not_changed"]}))


if __name__ == "__main__":
    main()
