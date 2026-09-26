from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, PLAN_STATES, STATES


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
        plan_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in PLAN_STATES)
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
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gate_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('available','maintenance')),
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS safety_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    max_flow REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    peak_at TEXT NOT NULL,
                    required_discharge REAL NOT NULL,
                    safety_flow REAL NOT NULL,
                    recommendation TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({plan_statuses})),
                    rationale TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_external_ref
                    ON plans(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS plan_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    actual_gates TEXT NOT NULL,
                    actual_discharge REAL NOT NULL,
                    recommended_discharge REAL NOT NULL,
                    deviation REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
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

    def create_gate(self, name: str, max_discharge: float, note: str,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO gates(name, max_discharge, note, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (name, max_discharge, note, actor, now),
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

    def add_gate_window(self, gate_id: int, kind: str, start_at: str, end_at: str,
                        note: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_gate(gate_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO gate_windows(gate_id, kind, start_at, end_at, note,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (gate_id, kind, start_at, end_at, note, actor, now),
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
        sql += " ORDER BY gate_id, start_at"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def create_safety_limit(self, start_at: str, end_at: str, max_flow: float,
                            note: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO safety_limits(start_at, end_at, max_flow, note,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (start_at, end_at, max_flow, note, actor, now),
            )
            limit_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM safety_limits WHERE id=?", (limit_id,)).fetchone()
        return dict(row)

    def list_safety_limits(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM safety_limits ORDER BY start_at").fetchall()
        return [dict(row) for row in rows]

    def create_plan(self, title: str, peak_at: str, required_discharge: float,
                    safety_flow: float, recommendation: Dict[str, Any], status: str,
                    external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO plans(title, peak_at, required_discharge, safety_flow,
                       recommendation, status, version, external_ref, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, peak_at, required_discharge, safety_flow,
                     json.dumps(recommendation, ensure_ascii=False, sort_keys=True),
                     status, 1, external_ref, actor, now, now),
                )
                plan_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("调度方案不存在")
        return dict(row)

    def list_plans(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM plans"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def transition_plan(self, plan_id: int, target: str, expected_version: int,
                        rationale: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if rationale is None:
                cur = self.conn.execute(
                    """UPDATE plans SET status=?, version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (target, now, plan_id, expected_version),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE plans SET status=?, rationale=?, version=version+1,
                       updated_at=? WHERE id=? AND version=?""",
                    (target, rationale, now, plan_id, expected_version),
                )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度方案不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_plan(plan_id)

    def execute_plan(self, plan_id: int, expected_version: int,
                     actual_gates: List[int], actual_discharge: float,
                     recommended_discharge: float, deviation: float,
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE plans SET status='executed', version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='authorized'""",
                (now, plan_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度方案不存在")
                raise ConflictError("方案状态或版本冲突，请刷新后重试")
            cur = self.conn.execute(
                """INSERT INTO plan_reports(plan_id, actual_gates, actual_discharge,
                   recommended_discharge, deviation, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (plan_id, json.dumps(actual_gates, ensure_ascii=False), actual_discharge,
                 recommended_discharge, deviation, actor, now),
            )
            report_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plan_reports WHERE id=?", (report_id,)).fetchone()
        report = dict(row)
        report["actual_gates"] = json.loads(report["actual_gates"])
        return report

    def list_plan_reports(self, plan_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM plan_reports"
        params: tuple = ()
        if plan_id is not None:
            sql += " WHERE plan_id=?"
            params = (plan_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            report = dict(row)
            report["actual_gates"] = json.loads(report["actual_gates"])
            result.append(report)
        return result

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

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        clauses = []
        params: list = []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
