from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    max_discharge REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gate_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    round_no INTEGER NOT NULL,
                    peak_arrival TEXT NOT NULL,
                    required_discharge REAL NOT NULL,
                    safety_flow REAL NOT NULL,
                    adjustment REAL NOT NULL DEFAULT 0,
                    target_discharge REAL NOT NULL,
                    recommended_discharge REAL NOT NULL,
                    gap REAL NOT NULL,
                    no_gate_available INTEGER NOT NULL DEFAULT 0,
                    over_safety INTEGER NOT NULL DEFAULT 0,
                    gates_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, round_no)
                );
                CREATE TABLE IF NOT EXISTS execution_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    recommendation_id INTEGER NOT NULL
                        REFERENCES recommendations(id) ON DELETE CASCADE,
                    gates_json TEXT NOT NULL,
                    actual_discharge REAL NOT NULL,
                    deviation REAL NOT NULL,
                    reported_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(recommendation_id)
                );
            """)
        columns = {row["name"] for row in
                   self.conn.execute("PRAGMA table_info(items)").fetchall()}
        if "safety_flow" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE items ADD COLUMN safety_flow REAL")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, safety_flow: Optional[float] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       safety_flow)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, safety_flow),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def create_gate(self, name: str, max_discharge: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO gates(name, max_discharge, created_by, created_at)"
                    " VALUES(?,?,?,?)",
                    (name, max_discharge, actor, now),
                )
                gate_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("闸门名称已存在") from exc
        return self.get_gate(gate_id)

    def get_gate(self, gate_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
        if row is None:
            raise NotFoundError("闸门不存在")
        return dict(row)

    def list_gates(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM gates ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def add_gate_window(self, gate_id: int, start_at: str, end_at: str,
                        reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_gate(gate_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO gate_windows(gate_id, start_at, end_at, reason,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (gate_id, start_at, end_at, reason, actor, now),
            )
            window_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_windows WHERE id=?", (window_id,)).fetchone()
        return dict(row)

    def list_gate_windows(self, gate_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM gate_windows"
        params: tuple = ()
        if gate_id is not None:
            sql += " WHERE gate_id=?"
            params = (gate_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["gates"] = json.loads(item.pop("gates_json"))
        item["no_gate_available"] = bool(item["no_gate_available"])
        item["over_safety"] = bool(item["over_safety"])
        return item

    def create_recommendation(self, item_id: int, data: Dict[str, Any],
                              actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(round_no),0) AS n FROM recommendations WHERE item_id=?",
                (item_id,)).fetchone()
            round_no = int(row["n"]) + 1
            cur = self.conn.execute(
                """INSERT INTO recommendations(item_id, round_no, peak_arrival,
                   required_discharge, safety_flow, adjustment, target_discharge,
                   recommended_discharge, gap, no_gate_available, over_safety,
                   gates_json, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, round_no, data["peak_arrival"], data["required_discharge"],
                 data["safety_flow"], data["adjustment"], data["target_discharge"],
                 data["recommended_discharge"], data["gap"],
                 1 if data["no_gate_available"] else 0,
                 1 if data["over_safety"] else 0,
                 json.dumps(data["gates"], ensure_ascii=False, sort_keys=True),
                 actor, now),
            )
            rec_id = int(cur.lastrowid)
        return self.get_recommendation(rec_id)

    def get_recommendation(self, rec_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        if row is None:
            raise NotFoundError("建议不存在")
        return self._recommendation(row)

    def list_recommendations(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM recommendations WHERE item_id=? ORDER BY round_no",
                (item_id,)).fetchall()
        return [self._recommendation(row) for row in rows]

    def latest_recommendation(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM recommendations WHERE item_id=?
                   ORDER BY round_no DESC LIMIT 1""", (item_id,)).fetchone()
        return self._recommendation(row) if row else None

    @staticmethod
    def _report(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["gates"] = json.loads(item.pop("gates_json"))
        return item

    def create_report(self, item_id: int, recommendation_id: int,
                      gates: List[Dict[str, Any]], actual_discharge: float,
                      deviation: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO execution_reports(item_id, recommendation_id,
                       gates_json, actual_discharge, deviation, reported_by, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (item_id, recommendation_id,
                     json.dumps(gates, ensure_ascii=False, sort_keys=True),
                     actual_discharge, deviation, actor, now),
                )
                report_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该轮建议已有执行回报") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM execution_reports WHERE id=?", (report_id,)).fetchone()
        return self._report(row)

    def list_reports(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM execution_reports WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [self._report(row) for row in rows]

    def report_for_recommendation(self, recommendation_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM execution_reports WHERE recommendation_id=?",
                (recommendation_id,)).fetchone()
        return self._report(row) if row else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()
