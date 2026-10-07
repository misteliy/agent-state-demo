"""Separate service transaction: validate grant, enforce contract, commit receipt.

The synchronous local call simulates a network service boundary. This service
owns the authoritative effect outcome, including durable no-effect rejections.
"""
import json
import uuid
from pathlib import Path

from authority import binding, verify_signature
from contracts import digest, validate
from store import clock, database, document


def order_scope(folder, order_id):
    # Trusted gateway metadata lookup; not an agent-visible unrestricted tool.
    with database(Path(folder) / "business.sqlite") as db:
        order = db.execute("SELECT tenant FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None:
        raise ValueError("Unknown order")
    return order["tenant"]


def lookup_outcome(folder, op):
    with database(Path(folder) / "business.sqlite") as db:
        row = db.execute("SELECT * FROM outcomes WHERE operation_id=?", (op["id"],)).fetchone()
    if row is None:
        return None
    if row["binding_digest"] != digest(binding(op)):
        raise ValueError("Outcome lookup binding mismatch")
    return json.loads(row["result"])


def execute(folder, op, now=None):
    folder = Path(folder)
    contract = document(folder, "contracts.json")
    policy = document(folder, "policy.json")
    args = json.loads(op["arguments"])
    validate(op["tool"], args, contract)
    if not op["grant"]:
        raise ValueError("No execution grant")
    payload = verify_signature(folder, json.loads(op["grant"]))
    expected = binding(op)
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("Execution grant does not match operation")
    if payload["contract_digest"] != digest(contract) or payload["policy_digest"] != digest(policy):
        raise ValueError("Contract or policy version mismatch")
    if op["tool"] == "issue_credit" and not payload.get("reservation_id"):
        raise ValueError("Credit requires a budget reservation")
    with database(folder / "business.sqlite") as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM outcomes WHERE operation_id=?", (op["id"],)).fetchone()
        if prior:
            if prior["binding_digest"] != digest(expected):
                raise ValueError("Operation identity reused with different payload")
            return json.loads(prior["result"])
        order = db.execute("SELECT * FROM orders WHERE id=?", (args["order_id"],)).fetchone()
        reason = None
        if payload["expires_at"] <= clock(now):
            reason = "execution grant expired"
        elif order is None or order["tenant"] != op["tenant"]:
            reason = "order or tenant mismatch"
        if op["tool"] != "issue_credit":
            if reason:
                raise ValueError(reason)
            if op["tool"] == "read_order":
                return {"order_id": order["id"], "delivery_fee_cents": order["delivery_fee_cents"],
                        "order_version": order["version"]}
            return {"delivered_late": bool(order["delivered_late"])}
        if reason is None:
            if order["version"] != args["expected_order_version"]:
                reason = "order version changed"
            elif not order["delivered_late"] or args["amount_cents"] != order["delivery_fee_cents"]:
                reason = "credit is not eligible"
            elif db.execute("SELECT 1 FROM credits WHERE order_id=?", (order["id"],)).fetchone():
                reason = "one-credit-per-order invariant"
        result = {"operation_id": op["id"], "binding_digest": digest(expected),
                  "effect_status": "rejected" if reason else "committed",
                  "order_id": args["order_id"], "amount_cents": args["amount_cents"]}
        if reason:
            # This durable rejection is also a tombstone. A delayed retry with
            # this ID cannot later commit, so its reservation can be released.
            result["reason"] = reason
        else:
            receipt = "receipt-" + uuid.uuid4().hex
            db.execute("INSERT INTO credits VALUES (?, ?, ?, ?, ?)",
                       (receipt, op["id"], order["id"], op["tenant"], args["amount_cents"]))
            result["receipt_id"] = receipt
        db.execute("INSERT INTO outcomes VALUES (?, ?, ?)",
                   (op["id"], digest(expected), json.dumps(result)))
        return result
