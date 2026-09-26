from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CHIEF_RATIONALE_KIND, CHIEF_RATIONALE_ROLES,
                    CREATE_ROLES, ENTITY, GATE_ROLES, RECOMMEND_ROLES,
                    RECORD_ROLES, REPORT_ROLES, TITLE, VIEW_ROLES,
                    allocate_gates, completion_blockers, escalation_required,
                    gate_available, parse_instant, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        safety_flow = payload.get("safety_flow")
        if safety_flow is not None:
            safety_flow = require_number(safety_flow, "safety_flow")
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, safety_flow)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        kind = require_text(payload.get("kind"), "kind", 100)
        if kind == CHIEF_RATIONALE_KIND:
            ensure_role(role, CHIEF_RATIONALE_ROLES)
        else:
            ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status, "role": role,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "authorized":
            self._check_authorization_gate(item_id)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def _check_authorization_gate(self, item_id: int) -> None:
        rec = self.repository.latest_recommendation(item_id)
        if rec is None or not (rec["no_gate_available"] or rec["over_safety"]):
            return
        rationale = [r for r in self.repository.list_records(item_id)
                     if r["kind"] == CHIEF_RATIONALE_KIND
                     and r["created_at"] >= rec["created_at"]]
        if not rationale:
            reasons = []
            if rec["no_gate_available"]:
                reasons.append("洪峰到达时无闸可用")
            if rec["over_safety"]:
                reasons.append("建议泄量越过下游安全流量")
            raise ConflictError(
                "；".join(reasons) + "，方案留在待复核，需总工写明取舍后才能授权")

    def register_gate(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        max_discharge = require_number(payload.get("max_discharge"), "max_discharge",
                                       0.000001)
        gate = self.repository.create_gate(name, max_discharge, actor)
        self.repository.append_audit("gate_register", "闸门", gate["id"], actor, {
            "name": name, "max_discharge": max_discharge,
        })
        return gate

    def add_gate_window(self, gate_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_ROLES)
        actor = require_text(actor, "actor", 100)
        start_at = require_text(payload.get("start_at"), "start_at", 100)
        end_at = require_text(payload.get("end_at"), "end_at", 100)
        reason = require_text(payload.get("reason"), "reason", 500)
        start = parse_instant(start_at, "start_at")
        end = parse_instant(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at必须晚于start_at")
        window = self.repository.add_gate_window(gate_id, start_at, end_at,
                                                 reason, actor)
        self.repository.append_audit("gate_window", "闸门", gate_id, actor, {
            "window_id": window["id"], "start_at": start_at, "end_at": end_at,
            "reason": reason,
        })
        return window

    def list_gates(self, role: str) -> list:
        self._view(role)
        windows: Dict[int, list] = {}
        for window in self.repository.list_gate_windows():
            windows.setdefault(window["gate_id"], []).append(window)
        result = []
        for gate in self.repository.list_gates():
            entry = dict(gate)
            entry["windows"] = windows.get(gate["id"], [])
            result.append(entry)
        return result

    def list_gate_windows(self, gate_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_gate_windows(gate_id)

    def recommend(self, item_id: int, payload: Dict[str, Any], actor: str,
                  role: str) -> Dict[str, Any]:
        ensure_role(role, RECOMMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        peak_raw = require_text(payload.get("peak_arrival"), "peak_arrival", 100)
        peak = parse_instant(peak_raw, "peak_arrival")
        required = require_number(payload.get("required_discharge"),
                                  "required_discharge")
        safety_flow = payload.get("safety_flow", item.get("safety_flow"))
        if safety_flow is None:
            raise ValidationError("下游安全流量未登记")
        safety_flow = require_number(safety_flow, "safety_flow")
        adjustment = 0.0
        previous = self.repository.latest_recommendation(item_id)
        if previous is not None:
            report = self.repository.report_for_recommendation(previous["id"])
            if report is not None:
                adjustment = round(previous["recommended_discharge"]
                                   - report["actual_discharge"], 3)
        target = round(max(0.0, required + adjustment), 3)
        windows: Dict[int, list] = {}
        for window in self.repository.list_gate_windows():
            windows.setdefault(window["gate_id"], []).append(window)
        available = []
        for gate in self.repository.list_gates():
            spans = [(parse_instant(w["start_at"], "start_at"),
                      parse_instant(w["end_at"], "end_at"))
                     for w in windows.get(gate["id"], [])]
            if gate_available(spans, peak):
                available.append(gate)
        total_available = round(sum(g["max_discharge"] for g in available), 3)
        recommended = round(min(target, total_available), 3)
        data = {
            "peak_arrival": peak_raw,
            "required_discharge": required,
            "safety_flow": safety_flow,
            "adjustment": adjustment,
            "target_discharge": target,
            "recommended_discharge": recommended,
            "gap": round(max(0.0, target - total_available), 3),
            "no_gate_available": len(available) == 0,
            "over_safety": recommended > safety_flow,
            "gates": allocate_gates(available, recommended),
        }
        rec = self.repository.create_recommendation(item_id, data, actor)
        self.repository.append_audit("recommend", ENTITY, item_id, actor, {
            "recommendation_id": rec["id"], "round_no": rec["round_no"],
            "recommended_discharge": recommended, "gap": rec["gap"],
            "no_gate_available": rec["no_gate_available"],
            "over_safety": rec["over_safety"],
        })
        return rec

    def list_recommendations(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_recommendations(item_id)

    def report_execution(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executed":
            raise ConflictError("只有已执行的方案才能回报实际开闸和泄量")
        rec_id = payload.get("recommendation_id")
        if rec_id is None:
            rec = self.repository.latest_recommendation(item_id)
            if rec is None:
                raise ValidationError("尚无建议可回报")
        else:
            if isinstance(rec_id, bool) or not isinstance(rec_id, int):
                raise ValidationError("recommendation_id必须是整数")
            rec = self.repository.get_recommendation(rec_id)
            if rec["item_id"] != item_id:
                raise ValidationError("建议不属于该方案")
        gates_payload = payload.get("gates")
        if not isinstance(gates_payload, list) or not gates_payload:
            raise ValidationError("gates必须是非空数组")
        entries = []
        actual = 0.0
        for entry in gates_payload:
            if not isinstance(entry, dict):
                raise ValidationError("gates元素必须是对象")
            gate = self.repository.get_gate(entry.get("gate_id"))
            discharge = require_number(entry.get("discharge"), "discharge")
            entries.append({"gate_id": gate["id"], "name": gate["name"],
                            "discharge": discharge})
            actual += discharge
        actual = round(actual, 3)
        deviation = round(rec["recommended_discharge"] - actual, 3)
        report = self.repository.create_report(item_id, rec["id"], entries,
                                               actual, deviation, actor)
        self.repository.append_audit("report", ENTITY, item_id, actor, {
            "report_id": report["id"], "recommendation_id": rec["id"],
            "actual_discharge": actual, "deviation": deviation,
        })
        return report

    def list_reports(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_reports(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
