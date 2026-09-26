from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='水库防汛调度与操作确认'; ENTITY='调度指令'; ID_PREFIX='RF'
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executed', 'closed']; TRANSITIONS={'draft': ['checked'], 'checked': ['authorized'], 'authorized': ['executed'], 'executed': ['closed'], 'closed': []}; TRANSITION_ROLES={'checked': ['duty_officer'], 'authorized': ['chief_engineer'], 'executed': ['dispatcher'], 'closed': ['chief_engineer']}
CREATE_ROLES=set(['duty_officer']); RECORD_ROLES=set(['duty_officer', 'dispatcher']); AUDIT_ROLES=set(['chief_engineer', 'viewer']); VIEW_ROLES=set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
SEVERITY_WEIGHT={'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
GATE_ROLES=set(['duty_officer']); RECOMMEND_ROLES=set(['duty_officer']); REPORT_ROLES=set(['dispatcher'])
CHIEF_RATIONALE_KIND='chief_rationale'; CHIEF_RATIONALE_ROLES=set(['chief_engineer'])
def parse_instant(value,field):
    from datetime import datetime, timezone
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    text=value.strip()
    if text.endswith('Z'): text=text[:-1]+'+00:00'
    try: moment=datetime.fromisoformat(text)
    except ValueError: raise ValidationError(f"{field}必须是ISO时间格式")
    if moment.tzinfo is None: moment=moment.replace(tzinfo=timezone.utc)
    return moment
def gate_available(windows,peak):
    for start,end in windows:
        if start<=peak<end: return False
    return True
def allocate_gates(gates,target):
    chosen=[]; remaining=max(0.0,float(target))
    for gate in sorted(gates,key=lambda g:(-g["max_discharge"],g["id"])):
        if remaining<=0: break
        discharge=min(gate["max_discharge"],remaining)
        if discharge>0:
            chosen.append({"gate_id":gate["id"],"name":gate["name"],"discharge":round(discharge,3)})
            remaining-=discharge
    return chosen
