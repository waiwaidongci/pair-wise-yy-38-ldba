from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError

# 闸门时段类型：available=可用时段，maintenance=检修（不可用）时段
WINDOW_KINDS = ("available", "maintenance")


def parse_instant(value: Any, field: str) -> str:
    """校验并归一化为UTC ISO字符串，保证可按字典序比较。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO8601时间") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def validate_span(start_at: str, end_at: str) -> None:
    if not start_at < end_at:
        raise ValidationError("开始时间必须早于结束时间")


def gate_status_at(windows: List[Dict[str, Any]], instant: str) -> Tuple[bool, str]:
    """某时刻闸门是否可用：须被可用时段覆盖，且未被检修时段覆盖。"""
    covered = False
    for window in windows:
        if window["start_at"] <= instant <= window["end_at"]:
            if window["kind"] == "maintenance":
                return False, "maintenance"
            covered = True
    return (True, "ok") if covered else (False, "no_window")


def safety_flow_at(limits: List[Dict[str, Any]], instant: str) -> Optional[float]:
    """某时刻下游安全流量：取覆盖该时刻的所有警戒值中最严格的一个。"""
    covering = [row["max_flow"] for row in limits
                if row["start_at"] <= instant <= row["end_at"]]
    return min(covering) if covering else None


def efficiency_factor(reports: List[Dict[str, Any]]) -> float:
    """按历史执行偏差（实际/建议）折算闸门有效泄量，只向下修正。"""
    ratios = [row["actual_discharge"] / row["recommended_discharge"]
              for row in reports if row["recommended_discharge"] > 0]
    if not ratios:
        return 1.0
    return max(0.5, min(1.0, sum(ratios) / len(ratios)))


def recommend(usable_gates: List[Dict[str, Any]], required: float,
              safety_flow: float, efficiency: float) -> Dict[str, Any]:
    """按洪峰所需泄量贪心选闸：有效泄量大的先开，泄量不超过安全流量。"""
    ordered = sorted(usable_gates, key=lambda g: g["max_discharge"], reverse=True)
    capacity = sum(g["max_discharge"] * efficiency for g in ordered)
    target = min(required, safety_flow)
    combination: List[Dict[str, Any]] = []
    allocated = 0.0
    for gate in ordered:
        if allocated >= target:
            break
        effective = round(gate["max_discharge"] * efficiency, 3)
        share = round(min(effective, target - allocated), 3)
        combination.append({
            "gate_id": gate["id"], "name": gate["name"],
            "allocated": share, "effective_max": effective,
        })
        allocated += share
    recommended = round(min(required, capacity, safety_flow), 3)
    gap = round(max(0.0, required - recommended), 3)
    flags = {
        "no_gate_available": not ordered,
        # 按洪峰需求泄放将越过下游安全流量，须总工取舍
        "exceeds_safety": required > safety_flow + 1e-9,
    }
    return {
        "combination": combination,
        "capacity": round(capacity, 3),
        "recommended_discharge": recommended,
        "gap": gap,
        "flags": flags,
    }


def needs_review(recommendation: Dict[str, Any]) -> bool:
    flags = recommendation["flags"]
    return bool(flags["no_gate_available"] or flags["exceeds_safety"])
