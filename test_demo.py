import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import authority
import demo
import service
import telemetry
from contracts import canonical
from store import checkpoint, database, operation, snapshot

T = 1_800_000_000
ARGS = {"order_id": "O-17", "amount_cents": 500, "expected_order_version": 1}


class GovernedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "case"

    def tearDown(self):
        self.temp.cleanup()

    def init(self, **kwargs):
        demo.initialize(self.folder, **kwargs)

    def totals(self):
        s = snapshot(self.folder)
        return (s["business"]["total_credit_cents"], s["authority"]["reserved_cents"], s["authority"]["spent_cents"])

    def prepare(self, args=None):
        return demo.prepare(self.folder, "issue_credit", args or ARGS, now=T)

    def new_independent_candidate(self, args=None):
        # Admission-component fixture, NOT a supported concurrent agent run.
        # The runtime itself deliberately assumes one worker owns its checkpoint.
        with database(self.folder / "runtime.sqlite") as db:
            db.execute("UPDATE checkpoint SET pending_operation=NULL WHERE id=1")
        return self.prepare(args)

    def test_crash_boundaries_in_new_processes(self):
        for boundary in ("after-admission", "after-dispatch", "after-credit", "after-ack"):
            with self.subTest(boundary=boundary):
                folder = Path(self.temp.name) / boundary
                demo.initialize(folder)
                demo.launch_worker(folder, boundary)
                before = snapshot(folder)
                if boundary == "after-credit":
                    self.assertEqual(before["business"]["total_credit_cents"], 500)
                    self.assertEqual(before["authority"]["reserved_cents"], 500)
                    self.assertNotIn("issue_credit", before["agent"]["facts"])
                # Correlated exports and observer playback are expendable.
                for filename in ("events.jsonl", "trace.otlp.json", "snapshots.jsonl"):
                    (folder / filename).unlink(missing_ok=True)
                demo.launch_worker(folder)
                after = snapshot(folder)
                self.assertEqual(after["runtime"]["status"], "finished")
                self.assertEqual(len(after["business"]["credits"]), 1)
                self.assertEqual(after["authority"]["reserved_cents"], 0)
                self.assertEqual(after["authority"]["spent_cents"], 500)
                self.assertEqual(len(after["authority"]["reservations"]), 1)
                demo.launch_worker(folder)
                self.assertEqual(snapshot(folder)["authority"]["spent_cents"], 500)

    def test_approval_pause_bind_and_resume(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        self.assertEqual(checkpoint(self.folder)["status"], "awaiting_approval")
        self.assertEqual(self.totals(), (0, 0, 0))
        approval_id = authority.approve(self.folder, now=T, ttl_seconds=60)
        demo.run_worker(self.folder, now=T+1)
        self.assertEqual(self.totals(), (1500, 0, 1500))
        with database(self.folder / "runtime.sqlite") as db:
            a = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            self.assertEqual(a["consumed_by"], a["operation_id"])

    def test_expiry_at_exact_boundary_blocks_new_effect(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        op_id = checkpoint(self.folder)["pending_operation"]
        authority.approve(self.folder, now=T, ttl_seconds=60)
        self.assertEqual(authority.admit(self.folder, op_id, now=T+59), "authorized")
        self.assertEqual(authority.admit(self.folder, op_id, now=T+60), "awaiting_approval")
        self.assertEqual(self.totals(), (0, 1500, 0))
        authority.approve(self.folder, now=T+61)
        demo.run_worker(self.folder, now=T+62)
        self.assertEqual(self.totals(), (1500, 0, 1500))
        self.assertEqual(len(snapshot(self.folder)["authority"]["reservations"]), 1)

    def test_committed_effect_reconciles_after_approval_expiry(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        authority.approve(self.folder, now=T, ttl_seconds=30)
        with self.assertRaises(SystemExit):
            demo.run_worker(self.folder, crash_at="after-credit", now=T+1)
        demo.run_worker(self.folder, now=T+100)
        self.assertEqual(self.totals(), (1500, 0, 1500))
        self.assertEqual(checkpoint(self.folder)["status"], "finished")

    def test_expired_grant_rejected_in_service_and_tombstoned(self):
        self.init()
        op_id = self.prepare()
        authority.admit(self.folder, op_id, now=T)
        op = operation(self.folder, op_id)
        result = service.execute(self.folder, op, now=T+300)
        self.assertEqual(result["effect_status"], "rejected")
        demo.acknowledge(self.folder, op, result, now=T+300)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.assertEqual(service.execute(self.folder, op, now=T+1), result)

    def test_approval_cannot_be_reused_for_new_operation(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        authority.approve(self.folder, now=T)
        new_id = self.new_independent_candidate({**ARGS, "amount_cents": 1500})
        self.assertEqual(authority.admit(self.folder, new_id, now=T+1), "awaiting_approval")

    def test_approval_arguments_cannot_change(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        authority.approve(self.folder, now=T)
        op_id = checkpoint(self.folder)["pending_operation"]
        with database(self.folder / "runtime.sqlite") as db:
            db.execute("UPDATE operations SET arguments=? WHERE id=?", (canonical({**ARGS, "amount_cents": 2000}), op_id))
        with self.assertRaisesRegex(ValueError, "binding changed"):
            authority.admit(self.folder, op_id, now=T+1)
        self.assertEqual(self.totals(), (0, 0, 0))

    def test_wrong_approver_and_wrong_tenant(self):
        self.init(case="approval")
        demo.run_worker(self.folder, now=T)
        with self.assertRaisesRegex(ValueError, "not a supervisor"):
            authority.approve(self.folder, "agent-other", now=T)
        other = Path(self.temp.name) / "other"
        demo.initialize(other, principal="agent-other")
        demo.run_worker(other)
        self.assertEqual(checkpoint(other)["status"], "denied")
        self.assertEqual(snapshot(other)["business"]["credits"], [])

    def test_budget_blocks_before_effect(self):
        self.init(budget_cents=400)
        demo.run_worker(self.folder)
        self.assertEqual(checkpoint(self.folder)["status"], "blocked_budget")
        self.assertEqual(self.totals(), (0, 0, 0))

    def test_two_admissions_cannot_spend_same_capacity(self):
        self.init(budget_cents=500)
        first = self.prepare()
        second = self.new_independent_candidate()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(authority.admit, self.folder, op_id, T) for op_id in (first, second)]
            statuses = [f.result() for f in futures]
        self.assertCountEqual(statuses, ["authorized", "blocked_budget"])
        self.assertEqual(self.totals(), (0, 500, 0))

    def test_retry_reserves_once_even_next_day(self):
        self.init()
        op_id = self.prepare()
        for when in (T, T+1, T+86400):
            self.assertEqual(authority.admit(self.folder, op_id, now=when), "authorized")
        self.assertEqual(self.totals(), (0, 500, 0))
        self.assertEqual(len(snapshot(self.folder)["authority"]["reservations"]), 1)

    def test_stale_precondition_releases_only_after_service_rejection(self):
        self.init(case="stale-order")
        demo.run_worker(self.folder)
        self.assertEqual(checkpoint(self.folder)["status"], "failed_no_effect")
        self.assertEqual(self.totals(), (0, 0, 0))
        reservation = snapshot(self.folder)["authority"]["reservations"][0]
        self.assertEqual(reservation["status"], "released")

    def test_business_invariant_rejects_new_duplicate_identity(self):
        self.init()
        demo.run_worker(self.folder, now=T)
        op_id = self.prepare()
        demo.dispatch(self.folder, op_id, now=T+1)
        self.assertEqual(self.totals(), (500, 0, 500))
        self.assertEqual(checkpoint(self.folder)["status"], "failed_no_effect")

    def test_service_deduplicates_concurrent_same_identity(self):
        self.init()
        op_id = self.prepare()
        authority.admit(self.folder, op_id, now=T)
        op = operation(self.folder, op_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.execute, self.folder, op, T+1) for _ in range(2)]
            results = [f.result() for f in futures]
        self.assertEqual(results[0], results[1])
        demo.acknowledge(self.folder, op, results[0], now=T+1)
        demo.acknowledge(self.folder, op, results[1], now=T+2)
        self.assertEqual(self.totals(), (500, 0, 500))

    def test_schema_rejects_bad_inputs_without_admission(self):
        self.init()
        for bad in ({**ARGS, "amount_cents": True}, {**ARGS, "amount_cents": -1},
                    {**ARGS, "extra": "x"}, {"order_id": "O-17"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.prepare(bad)
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            demo.prepare(self.folder, "delete_customer", ARGS)
        self.assertEqual(snapshot(self.folder)["runtime"]["operations"], [])

    def test_service_rejects_forged_grant(self):
        self.init()
        op_id = self.prepare()
        authority.admit(self.folder, op_id, now=T)
        op = operation(self.folder, op_id)
        grant = json.loads(op["grant"])
        grant["payload"]["expires_at"] = T+999999
        op["grant"] = json.dumps(grant)
        with self.assertRaisesRegex(ValueError, "signature"):
            service.execute(self.folder, op, now=T)
        self.assertEqual(self.totals(), (0, 500, 0))

    def test_fake_success_is_not_accepted_and_keeps_reservation(self):
        self.init()
        op_id = self.prepare()
        authority.admit(self.folder, op_id, now=T)
        op = operation(self.folder, op_id)
        with self.assertRaisesRegex(ValueError, "authoritative"):
            demo.acknowledge(self.folder, op, {"effect_status": "committed", "receipt_id": "invented"}, now=T)
        self.assertEqual(self.totals(), (0, 500, 0))
        demo.run_worker(self.folder, now=T+1)
        self.assertEqual(self.totals(), (500, 0, 500))

    def test_export_failure_does_not_fail_committed_operation(self):
        self.init()
        with patch("telemetry.export", side_effect=OSError("simulated disk error")):
            demo.run_worker(self.folder)
        self.assertEqual(self.totals(), (500, 0, 500))
        self.assertEqual(checkpoint(self.folder)["status"], "finished")

    def test_otlp_shape_and_event_correlation(self):
        self.init()
        demo.run_worker(self.folder)
        body = json.loads((self.folder / "trace.otlp.json").read_text())
        spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 4)
        for span in spans:
            self.assertRegex(span["traceId"], r"^[0-9a-f]{32}$")
            self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
            self.assertGreater(int(span["endTimeUnixNano"]), int(span["startTimeUnixNano"]))
        events = [json.loads(line) for line in (self.folder / "events.jsonl").read_text().splitlines()]
        credit = next(e["operation_id"] for e in events if e["kind"] == "budget.reserved")
        kinds = {e["kind"] for e in events if e["operation_id"] == credit}
        self.assertTrue({"contract.validated", "authorization.authorized", "budget.reserved", "tool.dispatched",
                         "tool.response", "verification.passed", "budget.spent", "operation.completed"} <= kinds)

    def test_dynamic_branches(self):
        for case, tools in [("on-time", {"read_order", "read_tracking"}), ("no-fee", {"read_order"})]:
            folder = Path(self.temp.name) / case
            demo.initialize(folder, case=case)
            demo.run_worker(folder)
            self.assertEqual(set(checkpoint(folder)["agent"]["facts"]), tools)
            self.assertEqual(snapshot(folder)["business"]["credits"], [])


if __name__ == "__main__":
    unittest.main()
