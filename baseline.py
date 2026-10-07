#!/usr/bin/env python3
"""Explore agent checkpoints, runtime bookkeeping and external effects.

Python standard library only. The decision function is a deterministic model
stand-in; no LLM or remote service is called. The SQLite commits and separate
worker-process restart are real. See README.md for the boundaries of the demo.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


CRASH_EXIT = 73


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(folder, mode="durable", case="late"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "config.json").write_text(json.dumps({"mode": mode, "case": case}))
    with database(folder / "runtime.sqlite") as db:
        db.executescript("""
            CREATE TABLE checkpoint (
                id INTEGER PRIMARY KEY, agent TEXT NOT NULL,
                pending_operation TEXT, finished INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE operations (
                id TEXT PRIMARY KEY, tool TEXT NOT NULL, arguments TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT
            );
        """)
        state = {
            "goal": "Resolve the delivery-fee complaint for order O-17.",
            "facts": {}, "intention": None, "answer": None,
        }
        db.execute("INSERT INTO checkpoint(id, agent) VALUES (1, ?)",
                   (json.dumps(state),))
    with database(folder / "business.sqlite") as db:
        db.executescript("""
            CREATE TABLE orders (
                id TEXT PRIMARY KEY, delivery_fee_cents INTEGER, delivered_late INTEGER
            );
            CREATE TABLE credits (
                receipt_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                order_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                arguments TEXT NOT NULL
            );
        """)
        db.execute("INSERT INTO orders VALUES ('O-17', ?, ?)",
                   (0 if case == "no-fee" else 500, int(case == "late")))
    capture(folder, "Goal received")


def read_checkpoint(folder):
    with database(folder / "runtime.sqlite") as db:
        row = db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone()
    return {"agent": json.loads(row["agent"]),
            "pending_operation": row["pending_operation"],
            "finished": bool(row["finished"])}


def capture(folder, label):
    """Observer-only export. Recovery NEVER reads this trace or the business DB.

    The observer can see both DBs so the learner can see a discrepancy that the
    agent cannot. Snapshots are taken between steps in this single-worker demo.
    """
    checkpoint = read_checkpoint(folder)
    with database(folder / "runtime.sqlite") as db:
        operations = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY rowid")]
    with database(folder / "business.sqlite") as db:
        credits = [dict(row) for row in db.execute(
            "SELECT receipt_id, operation_id, order_id, amount_cents FROM credits ORDER BY rowid")]
    snapshot = {
        "label": label, "agent": checkpoint["agent"],
        "runtime": {"pending_operation": checkpoint["pending_operation"],
                    "finished": checkpoint["finished"], "operations": operations},
        "business": {"credits": credits,
                     "total_credit_cents": sum(c["amount_cents"] for c in credits)},
    }
    with (folder / "snapshots.jsonl").open("a") as out:
        out.write(json.dumps(snapshot) + "\n")


def decide(state):
    """Transparent model stand-in; the runtime does not hardcode this sequence.

    There is no stored DAG or next-step index. Each choice depends on the facts
    available now. An LLM could replace this function and return the same shape.
    The demo's tiny action-selection policy is deliberately predefined.
    """
    facts = state["facts"]
    if "read_order" not in facts:
        return "read_order", {"order_id": "O-17"}
    if facts["read_order"]["delivery_fee_cents"] == 0:
        return "finish", {"answer": "There was no delivery fee to credit."}
    if "read_tracking" not in facts:
        return "read_tracking", {"order_id": "O-17"}
    if not facts["read_tracking"]["delivered_late"]:
        return "finish", {"answer": "Delivery was on time; the late-delivery credit does not apply."}
    if "issue_credit" not in facts:
        return "issue_credit", {"order_id": "O-17",
                                "amount_cents": facts["read_order"]["delivery_fee_cents"]}
    return "finish", {"answer": "One delivery-fee credit of EUR 5 was confirmed by the service."}


def service(folder, tool, arguments, operation_id):
    """Simulated external service. Dedupe and credit commit in ONE transaction.

    Same operation ID + changed payload is rejected. Distinct operation IDs
    remain distinct intents: this deliberately exposes the naive recovery bug.
    """
    with database(folder / "business.sqlite") as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM orders WHERE id=?",
                           (arguments["order_id"],)).fetchone()
        if order is None:
            raise ValueError("Unknown order")
        if tool == "read_order":
            return {"order_id": order["id"], "delivery_fee_cents": order["delivery_fee_cents"]}
        if tool == "read_tracking":
            return {"delivered_late": bool(order["delivered_late"])}
        if tool != "issue_credit":
            raise ValueError("Unknown tool")
        payload = json.dumps(arguments, sort_keys=True)
        old = db.execute("SELECT * FROM credits WHERE operation_id=?", (operation_id,)).fetchone()
        if old:
            if old["arguments"] != payload:
                raise ValueError("Operation identity reused with different arguments")
            return {"receipt_id": old["receipt_id"], "amount_cents": old["amount_cents"]}
        if (not order["delivered_late"] or arguments["amount_cents"] <= 0
                or arguments["amount_cents"] != order["delivery_fee_cents"]):
            raise ValueError("Credit is not eligible")
        receipt = "receipt-" + uuid.uuid4().hex[:8]
        db.execute("INSERT INTO credits VALUES (?, ?, ?, ?, ?)",
                   (receipt, operation_id, arguments["order_id"], arguments["amount_cents"], payload))
        return {"receipt_id": receipt, "amount_cents": arguments["amount_cents"]}


def lookup_receipt(folder, operation_id):
    """Service API, not privileged observer access by the recovering agent."""
    with database(folder / "business.sqlite") as db:
        row = db.execute("SELECT * FROM credits WHERE operation_id=?", (operation_id,)).fetchone()
    return None if row is None else {"receipt_id": row["receipt_id"], "amount_cents": row["amount_cents"]}


def acknowledge(folder, tool, result, operation_id, durable):
    # Agent's new observation and runtime's completed operation advance together.
    with database(folder / "runtime.sqlite") as db:
        row = db.execute("SELECT agent FROM checkpoint WHERE id=1").fetchone()
        state = json.loads(row["agent"])
        state["facts"][tool] = result
        state["intention"] = None
        if durable:
            db.execute("UPDATE operations SET status='completed', result=? WHERE id=?",
                       (json.dumps(result), operation_id))
        db.execute("UPDATE checkpoint SET agent=?, pending_operation=NULL WHERE id=1",
                   (json.dumps(state),))
    capture(folder, "Observation checkpointed: " + tool)


def run_worker(folder, crash_after_credit=False):
    folder = Path(folder)
    durable = json.loads((folder / "config.json").read_text())["mode"] == "durable"
    checkpoint = read_checkpoint(folder)
    if checkpoint["finished"]:
        return
    pending = checkpoint["pending_operation"]
    if durable and pending:
        with database(folder / "runtime.sqlite") as db:
            op = dict(db.execute("SELECT * FROM operations WHERE id=?", (pending,)).fetchone())
            db.execute("UPDATE operations SET status='outcome_unknown' WHERE id=?", (pending,))
        capture(folder, "Restart: reconcile the pending operation")
        result = lookup_receipt(folder, pending) if op["tool"] == "issue_credit" else None
        if result is None:
            # Safe here because this service atomically deduplicates this ID.
            # A missing receipt alone would NOT justify retrying a generic API.
            result = service(folder, op["tool"], json.loads(op["arguments"]), pending)
        acknowledge(folder, op["tool"], result, pending, True)
    elif not durable and checkpoint["agent"]["facts"]:
        capture(folder, "Restart: only the agent checkpoint is available")

    for _ in range(12):
        state = read_checkpoint(folder)["agent"]
        tool, arguments = decide(state)
        if tool == "finish":
            state["answer"] = arguments["answer"]
            state["intention"] = None
            with database(folder / "runtime.sqlite") as db:
                db.execute("UPDATE checkpoint SET agent=?, finished=1 WHERE id=1",
                           (json.dumps(state),))
            capture(folder, "Agent finished")
            return

        operation_id = "op-" + uuid.uuid4().hex[:8]
        state["intention"] = {"tool": tool, "arguments": arguments}
        with database(folder / "runtime.sqlite") as db:
            # Both modes save the agent's intention. Only durable mode preserves
            # execution identity and unresolved-operation bookkeeping.
            db.execute("UPDATE checkpoint SET agent=? WHERE id=1", (json.dumps(state),))
            if durable:
                db.execute("INSERT INTO operations VALUES (?, ?, ?, 'prepared', NULL)",
                           (operation_id, tool, json.dumps(arguments)))
                db.execute("UPDATE checkpoint SET pending_operation=? WHERE id=1", (operation_id,))
        capture(folder, "Agent selected: " + tool)
        if durable:
            with database(folder / "runtime.sqlite") as db:
                db.execute("UPDATE operations SET status='dispatched' WHERE id=?", (operation_id,))
        result = service(folder, tool, arguments, operation_id)
        if tool == "issue_credit" and crash_after_credit:
            # Service transaction has committed; result is not in runtime DB.
            # Observer snapshot doesn't participate in correctness or recovery.
            capture(folder, "Service committed; worker exits before saving the receipt")
            raise SystemExit(CRASH_EXIT)
        acknowledge(folder, tool, result, operation_id, durable)
    raise RuntimeError("Demo exceeded its turn budget")


def launch_worker(folder, crash=False):
    command = [sys.executable, str(Path(__file__).resolve()), "run", str(folder)]
    if crash:
        command.append("--crash-after-credit")
    result = subprocess.run(command, capture_output=True, text=True)
    expected = CRASH_EXIT if crash else 0
    if result.returncode != expected:
        raise RuntimeError(f"Expected exit {expected}, got {result.returncode}: {result.stderr}")


def demo(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    scenarios = [
        ("durable-crash", "durable", "late", True),
        ("checkpoint-only-crash", "checkpoint-only", "late", True),
        ("durable-normal", "durable", "late", False),
        ("on-time", "durable", "on-time", False),
        ("no-fee", "durable", "no-fee", False),
    ]
    exports = {}
    for name, mode, case, crash in scenarios:
        target = folder / name
        initialize(target, mode, case)
        launch_worker(target, crash)
        if crash:
            launch_worker(target)  # Fresh interpreter; no in-memory state.
        snapshots = [json.loads(line) for line in (target / "snapshots.jsonl").read_text().splitlines()]
        exports[name] = snapshots
        final = snapshots[-1]
        print(f"{name:24} credits={len(final['business']['credits'])} "
              f"total=EUR {final['business']['total_credit_cents'] / 100:.2f}")
    (folder / "playback.json").write_text(json.dumps(exports, indent=2))
    print(f"\nInspect the SQLite databases and snapshots in {folder.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("folder", type=Path)
    init.add_argument("--mode", choices=["durable", "checkpoint-only"], default="durable")
    init.add_argument("--case", choices=["late", "on-time", "no-fee"], default="late")
    run = sub.add_parser("run")
    run.add_argument("folder", type=Path)
    run.add_argument("--crash-after-credit", action="store_true")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("folder", type=Path)
    batch = sub.add_parser("demo")
    batch.add_argument("folder", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.folder, args.mode, args.case)
    elif args.command == "run":
        run_worker(args.folder, args.crash_after_credit)
    elif args.command == "inspect":
        snapshot = json.loads((args.folder / "snapshots.jsonl").read_text().splitlines()[-1])
        print(json.dumps(snapshot, indent=2))
    else:
        target = args.folder or Path(__file__).resolve().parent / "runs" / uuid.uuid4().hex[:8]
        demo(target)


if __name__ == "__main__":
    main()
