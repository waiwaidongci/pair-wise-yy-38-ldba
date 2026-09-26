import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import PLAN_ENTITY
from src.service import Service

PEAK = "2026-09-26T10:00:00+00:00"
DAY_START = "2026-09-26T00:00:00+00:00"
DAY_END = "2026-09-27T00:00:00+00:00"


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.g1 = self.service.create_gate(
            {"name": "1#闸", "max_discharge": 300}, "op", "duty_officer")
        self.g2 = self.service.create_gate(
            {"name": "2#闸", "max_discharge": 200}, "op", "duty_officer")
        for gate in (self.g1, self.g2):
            self.service.add_gate_window(gate["id"], {
                "kind": "available", "start_at": DAY_START, "end_at": DAY_END,
            }, "op", "duty_officer")
        self.service.create_safety_limit(
            {"start_at": DAY_START, "end_at": DAY_END, "max_flow": 1000,
             "note": "下游警戒"}, "op", "chief_engineer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _plan(self, required, ref):
        return self.service.create_plan({
            "title": "洪峰调度", "peak_at": PEAK, "required_discharge": required,
            "external_ref": ref,
        }, "op", "duty_officer")

    def test_clean_plan_combination_and_gap(self):
        plan = self._plan(400, "P-1")
        self.assertEqual(plan["status"], "draft")
        rec = plan["recommendation"]
        self.assertEqual(rec["recommended_discharge"], 400)
        self.assertEqual(rec["gap"], 0)
        self.assertEqual([c["gate_id"] for c in rec["combination"]],
                         [self.g1["id"], self.g2["id"]])
        self.assertEqual([c["allocated"] for c in rec["combination"]], [300, 100])

    def test_maintenance_blocks_gate_at_peak(self):
        self.service.add_gate_window(self.g2["id"], {
            "kind": "maintenance",
            "start_at": "2026-09-26T08:00:00+00:00",
            "end_at": "2026-09-26T12:00:00+00:00",
            "note": "检修",
        }, "op", "duty_officer")
        plan = self._plan(400, "P-2")
        rec = plan["recommendation"]
        self.assertEqual(plan["status"], "draft")
        self.assertEqual([g["id"] for g in rec["usable_gates"]], [self.g1["id"]])
        self.assertEqual(rec["blocked_gates"][0]["reason"], "maintenance")
        self.assertEqual(rec["recommended_discharge"], 300)
        self.assertEqual(rec["gap"], 100)

    def test_no_gate_available_goes_to_pending_review_and_needs_rationale(self):
        for gate in (self.g1, self.g2):
            self.service.add_gate_window(gate["id"], {
                "kind": "maintenance",
                "start_at": "2026-09-26T09:00:00+00:00",
                "end_at": "2026-09-26T11:00:00+00:00",
            }, "op", "duty_officer")
        plan = self._plan(400, "P-3")
        self.assertEqual(plan["status"], "pending_review")
        self.assertTrue(plan["recommendation"]["flags"]["no_gate_available"])
        with self.assertRaises(PermissionDenied):
            self.service.transition_plan(plan["id"], {
                "target": "authorized", "expected_version": plan["version"],
                "rationale": "越权",
            }, "attacker", "dispatcher")
        with self.assertRaises(ValidationError):
            self.service.transition_plan(plan["id"], {
                "target": "authorized", "expected_version": plan["version"],
            }, "chief", "chief_engineer")
        authorized = self.service.transition_plan(plan["id"], {
            "target": "authorized", "expected_version": plan["version"],
            "rationale": "洪峰不大，开启备用泵站位，取舍已核",
        }, "chief", "chief_engineer")
        self.assertEqual(authorized["status"], "authorized")
        self.assertIn("取舍", authorized["rationale"])

    def test_exceeds_safety_goes_to_pending_review(self):
        plan = self._plan(1200, "P-4")
        self.assertEqual(plan["status"], "pending_review")
        rec = plan["recommendation"]
        self.assertTrue(rec["flags"]["exceeds_safety"])
        self.assertEqual(rec["recommended_discharge"], 500)
        self.assertEqual(rec["gap"], 700)

    def test_missing_safety_limit_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create_plan({
                "title": "无警戒", "peak_at": "2026-09-28T10:00:00+00:00",
                "required_discharge": 100,
            }, "op", "duty_officer")

    def test_execution_report_adjusts_next_round(self):
        plan = self._plan(400, "P-5")
        authorized = self.service.transition_plan(plan["id"], {
            "target": "authorized", "expected_version": plan["version"],
        }, "chief", "chief_engineer")
        with self.assertRaises(PermissionDenied):
            self.service.report_execution(plan["id"], {
                "expected_version": authorized["version"], "actual_gates": [],
                "actual_discharge": 360,
            }, "chief", "chief_engineer")
        result = self.service.report_execution(plan["id"], {
            "expected_version": authorized["version"],
            "actual_gates": [self.g1["id"], self.g2["id"]],
            "actual_discharge": 360,
        }, "op", "dispatcher")
        self.assertEqual(result["plan"]["status"], "executed")
        self.assertEqual(result["report"]["deviation"], -40)
        self.assertAlmostEqual(result["next_efficiency"], 0.9)
        follow = self._plan(400, "P-6")
        rec = follow["recommendation"]
        self.assertAlmostEqual(rec["efficiency"], 0.9)
        self.assertEqual([c["allocated"] for c in rec["combination"]], [270, 130])
        self.assertEqual(rec["recommended_discharge"], 400)

    def test_plan_version_conflict_and_report_guards(self):
        plan = self._plan(100, "P-7")
        with self.assertRaises(ConflictError):
            self.service.transition_plan(plan["id"], {
                "target": "authorized", "expected_version": 99,
            }, "chief", "chief_engineer")
        with self.assertRaises(ConflictError):
            self.service.report_execution(plan["id"], {
                "expected_version": plan["version"], "actual_gates": [],
                "actual_discharge": 100,
            }, "op", "dispatcher")
        authorized = self.service.transition_plan(plan["id"], {
            "target": "authorized", "expected_version": plan["version"],
        }, "chief", "chief_engineer")
        with self.assertRaises(ValidationError):
            self.service.transition_plan(plan["id"], {
                "target": "executed", "expected_version": authorized["version"],
            }, "op", "dispatcher")

    def test_audit_chain_covers_dispatch_events(self):
        plan = self._plan(400, "P-8")
        authorized = self.service.transition_plan(plan["id"], {
            "target": "authorized", "expected_version": plan["version"],
        }, "chief", "chief_engineer")
        self.service.report_execution(plan["id"], {
            "expected_version": authorized["version"],
            "actual_gates": [self.g1["id"]], "actual_discharge": 400,
        }, "op", "dispatcher")
        events = self.service.audit("viewer", plan["id"], PLAN_ENTITY)
        actions = [e["action"] for e in events]
        self.assertEqual(actions, ["plan_create", "plan_transition", "plan_report"])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
