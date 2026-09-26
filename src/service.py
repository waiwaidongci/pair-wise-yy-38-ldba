from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .dispatch import (efficiency_factor, gate_status_at, needs_review,
                       parse_instant, recommend, safety_flow_at, validate_span,
                       WINDOW_KINDS)
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, GATE_ENTITY,
                    PLAN_CREATE_ROLES, PLAN_ENTITY, RECORD_ROLES,
                    REGISTER_ROLES, REPORT_ROLES, SAFETY_ENTITY, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    plan_role_for_transition, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_plan_transition, validate_transition)


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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
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
            "record_id": record["id"], "kind": kind, "status": status,
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
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
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

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

    # ---- 闸门与下游安全流量登记 ----

    def create_gate(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        max_discharge = require_number(
            payload.get("max_discharge"), "max_discharge", 0.000001)
        note = payload.get("note") or ""
        gate = self.repository.create_gate(name, max_discharge, str(note)[:500], actor)
        self.repository.append_audit("gate_create", GATE_ENTITY, gate["id"], actor, {
            "name": name, "max_discharge": max_discharge,
        })
        return gate

    def add_gate_window(self, gate_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = payload.get("kind")
        if kind not in WINDOW_KINDS:
            raise ValidationError("kind必须是available或maintenance")
        start_at = parse_instant(payload.get("start_at"), "start_at")
        end_at = parse_instant(payload.get("end_at"), "end_at")
        validate_span(start_at, end_at)
        note = payload.get("note") or ""
        window = self.repository.add_gate_window(
            gate_id, kind, start_at, end_at, str(note)[:500], actor)
        self.repository.append_audit("gate_window", GATE_ENTITY, gate_id, actor, {
            "window_id": window["id"], "kind": kind,
            "start_at": start_at, "end_at": end_at,
        })
        return window

    def list_gates(self, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        windows = self.repository.list_gate_windows()
        by_gate: Dict[int, list] = {}
        for window in windows:
            by_gate.setdefault(window["gate_id"], []).append(window)
        result = []
        for gate in self.repository.list_gates():
            entry = dict(gate)
            entry["windows"] = by_gate.get(gate["id"], [])
            result.append(entry)
        return result

    def create_safety_limit(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        start_at = parse_instant(payload.get("start_at"), "start_at")
        end_at = parse_instant(payload.get("end_at"), "end_at")
        validate_span(start_at, end_at)
        max_flow = require_number(payload.get("max_flow"), "max_flow", 0.000001)
        note = payload.get("note") or ""
        limit = self.repository.create_safety_limit(
            start_at, end_at, max_flow, str(note)[:500], actor)
        self.repository.append_audit("safety_limit", SAFETY_ENTITY, limit["id"], actor, {
            "start_at": start_at, "end_at": end_at, "max_flow": max_flow,
        })
        return limit

    def list_safety_limits(self, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        return self.repository.list_safety_limits()

    # ---- 调度方案：建议、复核授权、执行回报 ----

    def create_plan(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        peak_at = parse_instant(payload.get("peak_at"), "peak_at")
        required = require_number(
            payload.get("required_discharge"), "required_discharge", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)

        windows_by_gate: Dict[int, list] = {}
        for window in self.repository.list_gate_windows():
            windows_by_gate.setdefault(window["gate_id"], []).append(window)
        usable, blocked = [], []
        for gate in self.repository.list_gates():
            ok, reason = gate_status_at(windows_by_gate.get(gate["id"], []), peak_at)
            entry = {"id": gate["id"], "name": gate["name"],
                     "max_discharge": gate["max_discharge"]}
            if ok:
                usable.append(entry)
            else:
                blocked.append(dict(entry, reason=reason))

        safety = safety_flow_at(self.repository.list_safety_limits(), peak_at)
        if safety is None:
            raise ValidationError("洪峰到达时刻未登记下游安全流量")

        efficiency = efficiency_factor(self.repository.list_plan_reports())
        suggestion = recommend(usable, required, safety, efficiency)
        suggestion.update({
            "peak_at": peak_at, "required_discharge": required,
            "safety_flow": safety, "efficiency": round(efficiency, 4),
            "usable_gates": usable, "blocked_gates": blocked,
        })
        status = "pending_review" if needs_review(suggestion) else "draft"
        plan = self.repository.create_plan(
            title, peak_at, required, safety, suggestion, status, external_ref, actor)
        self.repository.append_audit("plan_create", PLAN_ENTITY, plan["id"], actor, {
            "peak_at": peak_at, "required_discharge": required, "status": status,
            "recommended_discharge": suggestion["recommended_discharge"],
            "gap": suggestion["gap"], "flags": suggestion["flags"],
        })
        return self.enrich_plan(plan)

    def transition_plan(self, plan_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        target = payload.get("target")
        expected = payload.get("expected_version")
        plan = self.repository.get_plan(plan_id)
        validate_plan_transition(plan["status"], target)
        if target == "executed":
            raise ValidationError("请通过执行回报接口完成执行")
        ensure_role(role, plan_role_for_transition(target))
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        rationale = None
        if plan["status"] == "pending_review" and target == "authorized":
            rationale = require_text(payload.get("rationale"), "rationale")
        updated = self.repository.transition_plan(plan_id, target, expected, rationale, actor)
        self.repository.append_audit("plan_transition", PLAN_ENTITY, plan_id, actor, {
            "from": plan["status"], "to": target,
            "rationale_recorded": rationale is not None,
        })
        return self.enrich_plan(updated)

    def report_execution(self, plan_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        plan = self.repository.get_plan(plan_id)
        if plan["status"] != "authorized":
            raise ConflictError("方案未授权，不能回报执行")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        actual_gates = payload.get("actual_gates", [])
        if (not isinstance(actual_gates, list)
                or any(not isinstance(g, int) or isinstance(g, bool) for g in actual_gates)):
            raise ValidationError("actual_gates必须是闸门编号列表")
        actual = require_number(payload.get("actual_discharge"), "actual_discharge")
        recommended = float(json.loads(plan["recommendation"])["recommended_discharge"])
        deviation = round(actual - recommended, 3)
        report = self.repository.execute_plan(
            plan_id, expected, actual_gates, actual, recommended, deviation, actor)
        next_efficiency = efficiency_factor(self.repository.list_plan_reports())
        self.repository.append_audit("plan_report", PLAN_ENTITY, plan_id, actor, {
            "report_id": report["id"], "actual_discharge": actual,
            "recommended_discharge": recommended, "deviation": deviation,
            "next_efficiency": round(next_efficiency, 4),
        })
        return {
            "plan": self.enrich_plan(self.repository.get_plan(plan_id)),
            "report": report,
            "next_efficiency": round(next_efficiency, 4),
        }

    def get_plan(self, plan_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_plan(self.repository.get_plan(plan_id))

    def list_plans(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich_plan(plan) for plan in self.repository.list_plans(status)]

    def list_plan_reports(self, plan_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_plan(plan_id)
        return self.repository.list_plan_reports(plan_id)

    @staticmethod
    def enrich_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(plan)
        result["recommendation"] = json.loads(plan["recommendation"])
        return result

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
