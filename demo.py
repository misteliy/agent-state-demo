#!/usr/bin/env python3
"""Governed agent execution with standard-library Python; no real LLM or money.

Policy, approval, reservations, service transactions and crash recovery execute.
Identities are local fixtures. See README for the security and durability limits.
"""
import argparse
import json
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
import authority
import baseline
import service
import telemetry
from contracts import canonical, digest, load, validate
from store import capture, checkpoint, config, database, document, event, operation, snapshot

CRASH_EXIT = 73


def initialize(folder, case="late", principal="agent-support", budget_cents=None):
    folder = Path(folder)
    policy, contract = load("policy.json"), load("contracts.json")
    if budget_cents is not None:
        if type(budget_cents) is not int or budget_cents < 0:
            raise ValueError("Budget must be a nonnegative integer")
        policy["daily_credit_budget_cents"] = budget_cents
    folder.mkdir(parents=True, exist_ok=False)
    cfg = {"run_id": uuid.uuid4().hex, "trace_id": uuid.uuid4().hex,
           "case": case, "principal": principal, "format_version": 2}
    for name, value in [("config.json", cfg), ("policy.json", policy), ("contracts.json", contract)]:
        (folder / name).write_text(json.dumps(value, indent=2))
    key = folder / "service.key"
    key.write_bytes(secrets.token_bytes(32))
    key.chmod(0o600)
    with database(folder / "runtime.sqlite") as db:
        db.executescript("""
            CREATE TABLE checkpoint (id INTEGER PRIMARY KEY, agent TEXT NOT NULL,
                pending_operation TEXT, status TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE operations (id TEXT PRIMARY KEY, tool TEXT NOT NULL, arguments TEXT NOT NULL,
                principal TEXT NOT NULL, tenant TEXT NOT NULL, contract_version TEXT NOT NULL,
                policy_version TEXT NOT NULL, contract_digest TEXT NOT NULL, policy_digest TEXT NOT NULL,
                binding_digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, grant TEXT);
            CREATE TABLE approvals (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                binding_digest TEXT NOT NULL, approver TEXT NOT NULL, expires_at REAL NOT NULL, consumed_by TEXT);
            CREATE TABLE reservations (id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                tenant TEXT NOT NULL, day TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('reserved','spent','released')));
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, time_ns INTEGER NOT NULL,
                kind TEXT NOT NULL, operation_id TEXT, attempt_id TEXT, attributes TEXT NOT NULL);
        """)
        state = {"goal": "Resolve the delivery-fee complaint for order O-17.", "facts": {}, "intention": None, "answer": None}
        db.execute("INSERT INTO checkpoint VALUES (1, ?, NULL, 'ready', 0)", (json.dumps(state),))
        event(db, "run.created", run_id=cfg["run_id"], model="rule-based fixture", principal=principal)
    with database(folder / "business.sqlite") as db:
        db.executescript("""
            CREATE TABLE orders (id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                delivery_fee_cents INTEGER NOT NULL, delivered_late INTEGER NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE credits (receipt_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                order_id TEXT UNIQUE NOT NULL, tenant TEXT NOT NULL, amount_cents INTEGER NOT NULL);
            CREATE TABLE outcomes (operation_id TEXT PRIMARY KEY, binding_digest TEXT NOT NULL, result TEXT NOT NULL);
        """)
        fee = 0 if case == "no-fee" else 1500 if case == "approval" else 500
        db.execute("INSERT INTO orders VALUES ('O-17', 'company-a', ?, ?, 1)", (fee, int(case != "on-time")))
    capture(folder, "Goal received")
    telemetry.safe_export(folder)


def decide(state):
    """Transparent selector; a real LLM could return this same tool/args shape."""
    facts = state["facts"]
    if "read_order" not in facts:
        return "read_order", {"order_id": "O-17"}
    order = facts["read_order"]
    if order["delivery_fee_cents"] == 0:
        return "finish", {"answer": "No delivery fee was charged."}
    if "read_tracking" not in facts:
        return "read_tracking", {"order_id": "O-17"}
    if not facts["read_tracking"]["delivered_late"]:
        return "finish", {"answer": "Delivery was on time; no late-delivery credit applies."}
    if "issue_credit" not in facts:
        return "issue_credit", {"order_id": "O-17", "amount_cents": order["delivery_fee_cents"],
                                "expected_order_version": order["order_version"]}
    result = facts["issue_credit"]
    return "finish", {"answer": f"Service confirmed EUR {result['amount_cents'] / 100:.2f} credit; receipt {result['receipt_id']}."}


def prepare(folder, tool, args, principal=None, now=None):
    folder = Path(folder)
    contract, policy = document(folder, "contracts.json"), document(folder, "policy.json")
    validate(tool, args, contract)
    op = {"id": "op-" + uuid.uuid4().hex, "tool": tool, "arguments": canonical(args),
          "principal": principal or config(folder)["principal"], "tenant": service.order_scope(folder, args["order_id"]),
          "contract_version": contract["version"], "policy_version": policy["version"],
          "contract_digest": digest(contract), "policy_digest": digest(policy)}
    op["binding_digest"] = digest(authority.binding(op))
    with database(folder / "runtime.sqlite") as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone()
        if current["pending_operation"]:
            raise ValueError("Resolve pending operation before choosing another")
        state = json.loads(current["agent"])
        state["intention"] = {"tool": tool, "arguments": args}
        db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL)",
                   tuple(op[key] for key in ["id", "tool", "arguments", "principal", "tenant", "contract_version",
                                            "policy_version", "contract_digest", "policy_digest", "binding_digest"]))
        db.execute("UPDATE checkpoint SET agent=?, pending_operation=?, status='prepared', finished=0 WHERE id=1",
                   (json.dumps(state), op["id"]))
        event(db, "agent.selected", op["id"], now=now, tool=tool, arguments_digest=digest(args))
        event(db, "contract.validated", op["id"], now=now, contract_version=contract["version"],
              validation="input schema; business preconditions checked in service transaction")
    capture(folder, "Agent selected: " + tool)
    return op["id"]


def acknowledge(folder, op, result, now=None):
    """Settle and checkpoint atomically, after matching authoritative evidence."""
    folder, failed = Path(folder), False
    saved = operation(folder, op["id"])
    if saved["status"] in ("completed", "failed_no_effect"):
        if json.loads(saved["result"]) != result:
            raise ValueError("Conflicting acknowledgement")
        return
    if op["tool"] == "issue_credit":
        actual = service.lookup_outcome(folder, op)
        if actual is None or actual != result:
            with database(folder / "runtime.sqlite") as db:
                db.execute("UPDATE operations SET status='verification_failed' WHERE id=?", (op["id"],))
                db.execute("UPDATE checkpoint SET status='verification_failed' WHERE id=1")
                event(db, "verification.failed", op["id"], now=now, reason="response differs from service outcome")
            capture(folder, "Verification failed; reservation retained")
            raise ValueError("Response does not match authoritative service outcome")
        failed = result["effect_status"] == "rejected"
    with database(folder / "runtime.sqlite") as db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT * FROM operations WHERE id=?", (op["id"],)).fetchone()
        if saved["status"] in ("completed", "failed_no_effect"):
            if json.loads(saved["result"]) != result:
                raise ValueError("Conflicting acknowledgement")
            return
        current = db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone()
        if current["pending_operation"] != op["id"]:
            raise ValueError("Operation does not own pending checkpoint")
        state = json.loads(current["agent"])
        state["intention"] = None
        if failed:
            state["answer"] = "Stopped without a new effect: " + result["reason"]
        else:
            state["facts"][op["tool"]] = result
        if op["tool"] == "issue_credit":
            reservation = db.execute("SELECT * FROM reservations WHERE operation_id=?", (op["id"],)).fetchone()
            if reservation is None or reservation["status"] != "reserved":
                raise ValueError("No active reservation to settle")
            settled = "released" if failed else "spent"
            db.execute("UPDATE reservations SET status=? WHERE operation_id=?", (settled, op["id"]))
            event(db, "budget." + settled, op["id"], now=now, reservation_id=reservation["id"], amount_cents=reservation["amount_cents"])
            event(db, "verification.passed", op["id"], now=now, evidence="service outcome lookup",
                  effect_status=result["effect_status"], receipt_id=result.get("receipt_id"))
        status = "failed_no_effect" if failed else "completed"
        db.execute("UPDATE operations SET status=?, result=? WHERE id=?", (status, json.dumps(result), op["id"]))
        db.execute("UPDATE checkpoint SET agent=?, pending_operation=NULL, status=?, finished=? WHERE id=1",
                   (json.dumps(state), "failed_no_effect" if failed else "ready", int(failed)))
        event(db, "operation." + status, op["id"], now=now)
    label = "Outcome recorded; reservation " + ("released" if failed else "settled") if op["tool"] == "issue_credit" else "Observation checkpointed: " + op["tool"]
    capture(folder, label)


def crash(folder, label):
    capture(folder, label)
    raise SystemExit(CRASH_EXIT)


def dispatch(folder, operation_id, crash_at=None, now=None):
    folder = Path(folder)
    if authority.admit(folder, operation_id, now=now) != "authorized":
        return False
    op = operation(folder, operation_id)
    is_credit = op["tool"] == "issue_credit"
    if is_credit and crash_at == "after-admission":
        crash(folder, "Crash after authorization and reservation; no dispatch")
    attempt_id = "attempt-" + uuid.uuid4().hex
    with database(folder / "runtime.sqlite") as db:
        db.execute("UPDATE operations SET status='dispatched' WHERE id=?", (operation_id,))
        db.execute("UPDATE checkpoint SET status='dispatched' WHERE id=1")
        event(db, "tool.dispatched", operation_id, attempt_id, now=now, tool=op["tool"],
              policy_version=op["policy_version"], contract_version=op["contract_version"])
    if is_credit and crash_at == "after-dispatch":
        crash(folder, "Crash after dispatch record; service outcome unknown to runtime")
    if is_credit and config(folder)["case"] == "stale-order":
        with database(folder / "business.sqlite") as db:
            db.execute("UPDATE orders SET version=version+1 WHERE id='O-17'")
    try:
        result = service.execute(folder, op, now=now)
    except Exception:
        with database(folder / "runtime.sqlite") as db:
            db.execute("UPDATE operations SET status='outcome_unknown' WHERE id=?", (operation_id,))
            db.execute("UPDATE checkpoint SET status='outcome_unknown' WHERE id=1")
            event(db, "operation.outcome_unknown", operation_id, attempt_id, now=now)
        raise
    if is_credit and crash_at == "after-credit":
        crash(folder, "Service committed; worker exits before saving the receipt")
    with database(folder / "runtime.sqlite") as db:
        event(db, "tool.response", operation_id, attempt_id, now=now, effect_status=result.get("effect_status", "read"))
    acknowledge(folder, op, result, now=now)
    if is_credit and crash_at == "after-ack":
        crash(folder, "Crash after receipt, settlement and agent checkpoint committed")
    return True


def run_worker(folder, crash_at=None, now=None):
    folder = Path(folder)
    try:
        current = checkpoint(folder)
        if current["finished"]:
            return
        pending = current["pending_operation"]
        if pending:
            op = operation(folder, pending)
            if op["status"] in ("dispatched", "outcome_unknown", "verification_failed"):
                with database(folder / "runtime.sqlite") as db:
                    db.execute("UPDATE operations SET status='outcome_unknown' WHERE id=?", (pending,))
                    db.execute("UPDATE checkpoint SET status='outcome_unknown' WHERE id=1")
                    event(db, "recovery.reconciling", pending, now=now)
                capture(folder, "Restart: reconcile; retain the existing reservation")
                outcome = service.lookup_outcome(folder, op) if op["tool"] == "issue_credit" else None
                if outcome is not None:
                    acknowledge(folder, op, outcome, now=now)
                elif not dispatch(folder, pending, crash_at=crash_at, now=now):
                    return
            elif not dispatch(folder, pending, crash_at=crash_at, now=now):
                return
        for _ in range(12):
            current = checkpoint(folder)
            if current["finished"]:
                return
            tool, args = decide(current["agent"])
            if tool == "finish":
                state = current["agent"]
                state["answer"], state["intention"] = args["answer"], None
                with database(folder / "runtime.sqlite") as db:
                    db.execute("UPDATE checkpoint SET agent=?, status='finished', finished=1 WHERE id=1", (json.dumps(state),))
                    event(db, "agent.finished", now=now)
                capture(folder, "Agent finished")
                return
            pending = prepare(folder, tool, args, now=now)
            if not dispatch(folder, pending, crash_at=crash_at, now=now):
                return
        raise RuntimeError("Demo exceeded its turn budget")
    finally:
        telemetry.safe_export(folder)


def launch_worker(folder, crash_at=None):
    command = [sys.executable, str(Path(__file__).resolve()), "run", str(folder)]
    if crash_at:
        command += ["--crash-at", crash_at]
    result = subprocess.run(command, capture_output=True, text=True)
    expected = CRASH_EXIT if crash_at else 0
    if result.returncode != expected:
        raise RuntimeError(f"Expected exit {expected}, got {result.returncode}: {result.stderr}")


def demo(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    cases = [("governed-crash", "late", "after-credit", None, "agent-support"),
             ("governed-normal", "late", None, None, "agent-support"),
             ("approval-pause", "approval", None, None, "agent-support"),
             ("budget-blocked", "late", None, 400, "agent-support"),
             ("wrong-tenant", "late", None, None, "agent-other"),
             ("stale-order", "stale-order", None, None, "agent-support"),
             ("on-time", "on-time", None, None, "agent-support"),
             ("no-fee", "no-fee", None, None, "agent-support")]
    exports, results = {}, []
    for name, case, failure, budget, principal in cases:
        target = folder / name
        initialize(target, case, principal, budget)
        launch_worker(target, failure)
        if failure:
            launch_worker(target)
        if case == "approval":
            assert checkpoint(target)["status"] == "awaiting_approval"
            authority.approve(target)  # Synthetic supervisor, explicitly part of demo.
            launch_worker(target)
        final = snapshot(target)
        row = {"scenario": name, "status": final["runtime"]["status"], "credits": len(final["business"]["credits"]),
               "total_cents": final["business"]["total_credit_cents"], "reserved_cents": final["authority"]["reserved_cents"],
               "spent_cents": final["authority"]["spent_cents"]}
        results.append(row)
        exports[name] = [json.loads(line) for line in (target / "snapshots.jsonl").read_text().splitlines()]
        print(f"{name:22} {row['status']:20} credit=EUR {row['total_cents']/100:5.2f} reserved={row['reserved_cents']:5} spent={row['spent_cents']:5} cents")
    old = folder / "baseline-checkpoint-only"
    baseline.initialize(old, mode="checkpoint-only")
    baseline.launch_worker(old, crash=True)
    baseline.launch_worker(old)
    exports["baseline-checkpoint-only"] = [json.loads(line) for line in (old / "snapshots.jsonl").read_text().splitlines()]
    print("baseline-checkpoint-only   (original ungoverned counterexample) credit=EUR 10.00")
    (folder / "playback.json").write_text(json.dumps(exports, indent=2))
    (folder / "results.json").write_text(json.dumps(results, indent=2))
    print(f"\nEvidence and traces: {folder.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("folder", type=Path)
    init.add_argument("--case", choices=["late", "on-time", "no-fee", "approval", "stale-order"], default="late")
    init.add_argument("--principal", default="agent-support")
    init.add_argument("--budget-cents", type=int)
    run = sub.add_parser("run")
    run.add_argument("folder", type=Path)
    run.add_argument("--crash-at", choices=["after-admission", "after-dispatch", "after-credit", "after-ack"])
    run.add_argument("--crash-after-credit", action="store_true", help="Alias for --crash-at after-credit")
    approve = sub.add_parser("approve")
    approve.add_argument("folder", type=Path)
    approve.add_argument("--as", dest="supervisor", default="supervisor-a", help="SIMULATED trusted identity")
    approve.add_argument("--ttl-seconds", type=int, default=3600)
    for name in ("inspect", "export"):
        sub.add_parser(name).add_argument("folder", type=Path)
    sub.add_parser("demo").add_argument("folder", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.folder, args.case, args.principal, args.budget_cents)
    elif args.command == "run":
        run_worker(args.folder, "after-credit" if args.crash_after_credit else args.crash_at)
        print(checkpoint(args.folder)["status"])
    elif args.command == "approve":
        print(authority.approve(args.folder, args.supervisor, args.ttl_seconds))
        telemetry.safe_export(args.folder)
    elif args.command == "inspect":
        print(json.dumps(snapshot(args.folder), indent=2))
    elif args.command == "export":
        telemetry.export(args.folder)
    else:
        demo(args.folder or Path(__file__).resolve().parent / "runs" / uuid.uuid4().hex[:8])


if __name__ == "__main__":
    main()
