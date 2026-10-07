import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import baseline as demo


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "case"

    def tearDown(self):
        self.temp.cleanup()

    def credits(self):
        with demo.database(self.folder / "business.sqlite") as db:
            return [dict(row) for row in db.execute("SELECT * FROM credits")]

    def test_real_process_restart_reconciles_committed_credit(self):
        demo.initialize(self.folder)
        demo.launch_worker(self.folder, crash=True)
        state = demo.read_checkpoint(self.folder)
        self.assertNotIn("issue_credit", state["agent"]["facts"])
        self.assertEqual(state["agent"]["intention"]["tool"], "issue_credit")
        self.assertEqual(len(self.credits()), 1)
        self.assertEqual(state["pending_operation"], self.credits()[0]["operation_id"])
        with demo.database(self.folder / "runtime.sqlite") as db:
            self.assertEqual(db.execute("SELECT status FROM operations WHERE id=?",
                                        (state["pending_operation"],)).fetchone()[0], "dispatched")
        # No recovery dependency on the observer trace.
        (self.folder / "snapshots.jsonl").unlink()
        demo.launch_worker(self.folder)
        self.assertEqual(len(self.credits()), 1)
        state = demo.read_checkpoint(self.folder)
        self.assertTrue(state["finished"])
        self.assertIsNone(state["pending_operation"])
        self.assertIn("issue_credit", state["agent"]["facts"])
        demo.launch_worker(self.folder)
        self.assertEqual(len(self.credits()), 1)

    def test_agent_checkpoint_and_intention_alone_can_duplicate(self):
        demo.initialize(self.folder, mode="checkpoint-only")
        demo.launch_worker(self.folder, crash=True)
        self.assertEqual(len(self.credits()), 1)
        self.assertEqual(demo.read_checkpoint(self.folder)["agent"]["intention"]["tool"],
                         "issue_credit")
        demo.launch_worker(self.folder)
        self.assertEqual(len(self.credits()), 2)
        self.assertEqual(sum(c["amount_cents"] for c in self.credits()), 1000)
        self.assertNotEqual(self.credits()[0]["operation_id"], self.credits()[1]["operation_id"])

    def test_normal_execution(self):
        demo.initialize(self.folder)
        demo.launch_worker(self.folder)
        self.assertEqual(len(self.credits()), 1)
        self.assertTrue(demo.read_checkpoint(self.folder)["finished"])

    def test_on_time_observation_changes_trajectory(self):
        demo.initialize(self.folder, case="on-time")
        demo.launch_worker(self.folder)
        state = demo.read_checkpoint(self.folder)
        self.assertIn("read_tracking", state["agent"]["facts"])
        self.assertNotIn("issue_credit", state["agent"]["facts"])
        self.assertEqual(self.credits(), [])

    def test_no_fee_skips_tracking(self):
        demo.initialize(self.folder, case="no-fee")
        demo.launch_worker(self.folder)
        state = demo.read_checkpoint(self.folder)
        self.assertNotIn("read_tracking", state["agent"]["facts"])
        self.assertEqual(self.credits(), [])

    def test_service_deduplicates_concurrent_same_operation(self):
        demo.initialize(self.folder)
        args = {"order_id": "O-17", "amount_cents": 500}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(demo.service, self.folder, "issue_credit", args, "same-op")
                       for _ in range(2)]
            results = [f.result() for f in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.credits()), 1)

    def test_service_rejects_changed_payload_for_same_identity(self):
        demo.initialize(self.folder)
        demo.service(self.folder, "issue_credit", {"order_id": "O-17", "amount_cents": 500}, "op-x")
        with self.assertRaisesRegex(ValueError, "different arguments"):
            demo.service(self.folder, "issue_credit", {"order_id": "O-17", "amount_cents": 400}, "op-x")
        self.assertEqual(len(self.credits()), 1)


if __name__ == "__main__":
    unittest.main()
