"""Small durable store: task truth is separate from chat and user memory."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

LOCK = RLock()
KST = timezone(timedelta(hours=9))


def now():
    return datetime.now(KST).isoformat(timespec="seconds")


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS objects (collection TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(collection,id))")

    @contextmanager
    def connect(self):
        # sqlite3.Connection's own context manager commits but does not close.
        # Explicit closure is required before expiring a visitor workspace on
        # Windows and avoids retaining a descriptor per MCP authorization read.
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, collection, object_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM objects WHERE collection=? AND id=?", (collection, object_id)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, collection):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM objects WHERE collection=? ORDER BY rowid DESC", (collection,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, collection, obj):
        with LOCK, self.connect() as db:
            db.execute("INSERT INTO objects VALUES (?,?,?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body",
                       (collection, obj["id"], json.dumps(obj, ensure_ascii=False, default=str)))
        return obj

    def update(self, collection, object_id, **changes):
        with LOCK:
            obj = self.get(collection, object_id)
            if obj is None:
                raise KeyError(object_id)
            obj.update(changes)
            return self.put(collection, obj)

    def delete(self, collection, object_id):
        with LOCK, self.connect() as db:
            db.execute("DELETE FROM objects WHERE collection=? AND id=?", (collection, object_id))

    def event(self, task_id, message):
        with LOCK:
            task = self.get("tasks", task_id)
            task["events"].append({"at": now(), "message": message})
            task["updated_at"] = now()
            self.put("tasks", task)

    def memory(self, key, value, source="사용자 입력"):
        with LOCK:
            existing = next((m for m in self.all("memories") if m["key"] == key), None)
            return self.put("memories", {"id": existing["id"] if existing else uid("memory"), "key": key,
                                       "value": value, "source": source, "updated_at": now()})

    def notify(self, key, title, message, task_id=None):
        with LOCK:
            if self.get("notifications", key):
                return False
            self.put("notifications", {"id": key, "title": title, "message": message,
                                      "task_id": task_id, "created_at": now(), "read": False})
            return True

    def seed(self):
        if self.get("meta", "seed"):
            return
        self.memory("기본 UAT 양식", "요구사항 / 사전조건 / 절차 / 기대결과 / 실제결과 / 화면증거 / 판정", "기본 설정")
        self.memory("업무 우선순위", "마감이 가까운 업무와 검증에 실패한 업무를 먼저 확인", "기본 설정")
        self.put("meta", {"id": "seed", "created_at": now()})

