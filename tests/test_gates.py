import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import CHIEF_RATIONALE_KIND, STATES, TRANSITION_ROLES
from src.service import Service

PEAK = "2026-07-20T08:00:00+00:00"


class GateSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "7.20洪峰调度", "description": "闸门检修与下游警戒并存",
             "severity": "urgent", "quantity": 12, "threshold": 6,
             "external_ref": "FL-1", "safety_flow": 500},
            "duty1", "duty_officer")
        self.g1 = self.service.register_gate(
            {"name": "1号闸", "max_discharge": 300}, "duty1", "duty_officer")
        self.g2 = self.service.register_gate(
            {"name": "2号闸", "max_discharge": 200}, "duty1", "duty_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _advance(self, item, *targets):
        current = item
        for target in targets:
            current = self.service.transition(
                current["id"], target, current["version"], "actor",
                TRANSITION_ROLES[target][0])
        return current

    def test_recommendation_combination_and_gap(self):
        rec = self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 1000},
            "duty1", "duty_officer")
        self.assertEqual(rec["round_no"], 1)
        self.assertEqual(rec["recommended_discharge"], 500)
        self.assertEqual(rec["gap"], 500)
        self.assertFalse(rec["no_gate_available"])
        self.assertFalse(rec["over_safety"])
        self.assertEqual([g["gate_id"] for g in rec["gates"]],
                         [self.g1["id"], self.g2["id"]])
        self.assertEqual(sum(g["discharge"] for g in rec["gates"]), 500)

    def test_maintenance_window_excludes_gate(self):
        self.service.add_gate_window(
            self.g1["id"],
            {"start_at": "2026-07-20T00:00:00+00:00",
             "end_at": "2026-07-21T00:00:00+00:00", "reason": "闸门大修"},
            "duty1", "duty_officer")
        rec = self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 400},
            "duty1", "duty_officer")
        self.assertEqual(rec["recommended_discharge"], 200)
        self.assertEqual(rec["gap"], 200)
        self.assertEqual([g["gate_id"] for g in rec["gates"]], [self.g2["id"]])

    def test_no_gate_available_requires_chief_rationale(self):
        for gate in (self.g1, self.g2):
            self.service.add_gate_window(
                gate["id"],
                {"start_at": "2026-07-20T00:00:00+00:00",
                 "end_at": "2026-07-21T00:00:00+00:00", "reason": "检修"},
                "duty1", "duty_officer")
        rec = self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 300},
            "duty1", "duty_officer")
        self.assertTrue(rec["no_gate_available"])
        current = self._advance(self.service.get_item(self.item["id"], "viewer"),
                                STATES[1])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[2], current["version"],
                                    "chief1", "chief_engineer")
        with self.assertRaises(PermissionDenied):
            self.service.add_record(
                self.item["id"],
                {"kind": CHIEF_RATIONALE_KIND, "detail": "越权取舍", "status": "closed"},
                "disp1", "dispatcher")
        self.service.add_record(
            self.item["id"],
            {"kind": CHIEF_RATIONALE_KIND,
             "detail": "洪峰到达时两闸均在检修，改为预泄并请求上游错峰，风险自担",
             "status": "closed"},
            "chief1", "chief_engineer")
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], STATES[2],
                                          current["version"], "chief1",
                                          "chief_engineer")
        self.assertEqual(current["status"], STATES[2])

    def test_over_safety_requires_chief_rationale(self):
        rec = self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 600, "safety_flow": 400},
            "duty1", "duty_officer")
        self.assertTrue(rec["over_safety"])
        self.assertEqual(rec["recommended_discharge"], 500)
        current = self._advance(self.service.get_item(self.item["id"], "viewer"),
                                STATES[1])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[2], current["version"],
                                    "chief1", "chief_engineer")
        self.service.add_record(
            self.item["id"],
            {"kind": CHIEF_RATIONALE_KIND,
             "detail": "下游警戒400，为保库安全短时越限100，已通知下游转移",
             "status": "closed"},
            "chief1", "chief_engineer")
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], STATES[2],
                                          current["version"], "chief1",
                                          "chief_engineer")
        self.assertEqual(current["status"], STATES[2])

    def test_report_and_deviation_adjusts_next_round(self):
        rec = self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 400},
            "duty1", "duty_officer")
        self.assertEqual(rec["recommended_discharge"], 400)
        current = self._advance(self.service.get_item(self.item["id"], "viewer"),
                                *STATES[1:4])
        with self.assertRaises(PermissionDenied):
            self.service.report_execution(
                self.item["id"],
                {"gates": [{"gate_id": self.g1["id"], "discharge": 250}]},
                "duty1", "duty_officer")
        report = self.service.report_execution(
            self.item["id"],
            {"gates": [{"gate_id": self.g1["id"], "discharge": 250},
                       {"gate_id": self.g2["id"], "discharge": 100}]},
            "disp1", "dispatcher")
        self.assertEqual(report["actual_discharge"], 350)
        self.assertEqual(report["deviation"], 50)
        with self.assertRaises(ConflictError):
            self.service.report_execution(
                self.item["id"],
                {"gates": [{"gate_id": self.g1["id"], "discharge": 400}]},
                "disp1", "dispatcher")
        nxt = self.service.recommend(
            self.item["id"],
            {"peak_arrival": "2026-07-21T08:00:00+00:00",
             "required_discharge": 400},
            "duty1", "duty_officer")
        self.assertEqual(nxt["round_no"], 2)
        self.assertEqual(nxt["adjustment"], 50)
        self.assertEqual(nxt["target_discharge"], 450)
        self.assertEqual(nxt["recommended_discharge"], 450)

    def test_report_requires_executed_status(self):
        self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 300},
            "duty1", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.report_execution(
                self.item["id"],
                {"gates": [{"gate_id": self.g1["id"], "discharge": 100}]},
                "disp1", "dispatcher")

    def test_registration_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_gate({"name": "3号闸", "max_discharge": 100},
                                       "viewer1", "viewer")
        with self.assertRaises(ConflictError):
            self.service.register_gate({"name": "1号闸", "max_discharge": 100},
                                       "duty1", "duty_officer")
        with self.assertRaises(ValidationError):
            self.service.add_gate_window(
                self.g1["id"],
                {"start_at": "2026-07-21T00:00:00+00:00",
                 "end_at": "2026-07-20T00:00:00+00:00", "reason": "窗口倒置"},
                "duty1", "duty_officer")
        with self.assertRaises(ValidationError):
            self.service.recommend(
                self.item["id"],
                {"peak_arrival": "not-a-time", "required_discharge": 100},
                "duty1", "duty_officer")
        plain = self.service.create_item(
            {"title": "未登记安全流量", "description": "无safety_flow",
             "severity": "attention", "quantity": 1, "threshold": 10,
             "external_ref": "FL-2"},
            "duty1", "duty_officer")
        with self.assertRaises(ValidationError):
            self.service.recommend(
                plain["id"],
                {"peak_arrival": PEAK, "required_discharge": 100},
                "duty1", "duty_officer")

    def test_audit_chain_intact(self):
        self.service.recommend(
            self.item["id"],
            {"peak_arrival": PEAK, "required_discharge": 300},
            "duty1", "duty_officer")
        self._advance(self.service.get_item(self.item["id"], "viewer"), *STATES[1:4])
        self.service.report_execution(
            self.item["id"],
            {"gates": [{"gate_id": self.g1["id"], "discharge": 300}]},
            "disp1", "dispatcher")
        self.assertTrue(self.repo.verify_audit_chain())
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        self.assertIn("recommend", actions)
        self.assertIn("report", actions)


if __name__ == "__main__":
    unittest.main()
