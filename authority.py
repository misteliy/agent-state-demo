"""Illustrative policy evaluator, approval binding and atomic budget admission.

This is plain Python, NOT Dogwood. Identities and signing keys are local fixtures,
not a production authentication or credential-isolation implementation.
"""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contracts import canonical, digest, validate
from store import capture, checkpoint, clock, config, database, document, event, operation


def binding(op):
    return {"operation_id": op["id"], "principal": op["principal"], "tenant": op["tenant"],
            "tool": op["tool"], "arguments_digest": digest(json.loads(op["arguments"])),
            "contract_digest": op["contract_digest"], "policy_digest": op["policy_digest"]}


def validate_binding(folder, op):
    contract, policy = document(folder, "contracts.json"), document(folder, "policy.json")
    validate(op["tool"], json.loads(op["arguments"]), contract)
    if op["contract_digest"] != digest(contract) or op["policy_digest"] != digest(policy):
        raise ValueError("Pinned contract or policy changed; migration is not implemented")
    if digest(binding(op)) != op["binding_digest"]:
        raise ValueError("Operation binding changed")


def sign(folder, payload):
    key = (Path(folder) / "service.key").read_bytes()
    return {"payload": payload, "signature": hmac.new(key, canonical(payload).encode(), hashlib.sha256).hexdigest()}


def verify_signature(folder, grant):
    expected = sign(folder, grant["payload"])["signature"]
    if not hmac.compare_digest(grant["signature"], expected):
        raise ValueError("Invalid execution grant signature")
    return grant["payload"]


def approve(folder, supervisor="supervisor-a", ttl_seconds=3600, now=None):
    """CLI --as is a SIMULATED trusted operator identity, not authentication."""
    now = clock(now)
    if ttl_seconds <= 0:
        raise ValueError("Approval TTL must be positive")
    pending = checkpoint(folder)["pending_operation"]
    if not pending:
        raise ValueError("No pending operation to approve")
    op = operation(folder, pending)
    if op["tool"] != "issue_credit" or op["status"] != "awaiting_approval":
        raise ValueError("Operation is not awaiting credit approval")
    validate_binding(folder, op)
    policy = document(folder, "policy.json")
    principal = policy["principals"].get(supervisor, {})
    if principal.get("role") != "supervisor" or principal.get("tenant") != op["tenant"]:
        raise ValueError("Approver is not a supervisor for this tenant")
    approval_id = "approval-" + uuid.uuid4().hex
    with database(Path(folder) / "runtime.sqlite") as db:
        db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, NULL)",
                   (approval_id, pending, op["binding_digest"], supervisor, now + ttl_seconds))
        event(db, "approval.granted", pending, now=now, approval_id=approval_id,
              binding_digest=op["binding_digest"], approver=supervisor, expires_at=now + ttl_seconds)
    capture(folder, "Supervisor approved the exact operation")
    return approval_id


def admit(folder, operation_id, now=None):
    """Serialize policy checks, approval binding, reservation and grant issuance.

    Budget scope is tenant/day within this runtime DB, not across all demo folders.
    Existing reservations retain their original day when a run resumes tomorrow.
    """
    folder = Path(folder)
    policy = document(folder, "policy.json")
    with database(folder / "runtime.sqlite") as db:
        db.execute("BEGIN IMMEDIATE")
        now = clock(now)  # Sample after lock acquisition, not before contention.
        op = dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
        validate_binding(folder, op)
        if op["status"] in ("completed", "failed_no_effect"):
            return op["status"]
        person = policy["principals"].get(op["principal"], {})
        allowed = person.get("role") == "agent" and person.get("tenant") == op["tenant"]
        status, reason = "authorized", "within delegated scope"
        approval, reservation = None, None
        if not allowed:
            status, reason = "denied", "principal is outside the tenant or role scope"
        elif op["tool"] == "issue_credit":
            amount = json.loads(op["arguments"])["amount_cents"]
            if amount > policy["automatic_credit_limit_cents"]:
                approval = db.execute("""SELECT * FROM approvals
                    WHERE operation_id=? AND binding_digest=? AND expires_at>?
                    AND (consumed_by IS NULL OR consumed_by=?) ORDER BY expires_at DESC LIMIT 1""",
                    (op["id"], op["binding_digest"], now, op["id"])).fetchone()
                if not approval:
                    status, reason = "awaiting_approval", "exact-operation supervisor approval required"
            if status == "authorized":
                reservation = db.execute("SELECT * FROM reservations WHERE operation_id=?", (op["id"],)).fetchone()
                if reservation and (reservation["status"] != "reserved" or reservation["amount_cents"] != amount):
                    raise ValueError("Existing reservation is not reusable")
                if not reservation:
                    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
                    used = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM reservations
                        WHERE tenant=? AND day=? AND status IN ('reserved','spent')""", (op["tenant"], day)).fetchone()[0]
                    if used + amount > policy["daily_credit_budget_cents"]:
                        status, reason = "blocked_budget", "insufficient unreserved daily effect budget"
                    else:
                        reservation_id = "reservation-" + uuid.uuid4().hex
                        db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?, 'reserved')",
                                   (reservation_id, op["id"], op["tenant"], day, amount))
                        reservation = db.execute("SELECT * FROM reservations WHERE operation_id=?", (op["id"],)).fetchone()
                        event(db, "budget.reserved", op["id"], now=now, reservation_id=reservation_id,
                              amount_cents=amount, tenant=op["tenant"], day=day)
        grant = None
        if status == "authorized":
            expires = now + policy["grant_lifetime_seconds"]
            if approval:
                expires = min(expires, approval["expires_at"])
                if approval["consumed_by"] is None:
                    db.execute("UPDATE approvals SET consumed_by=? WHERE id=?", (op["id"], approval["id"]))
                    event(db, "approval.bound", op["id"], now=now, approval_id=approval["id"])
            payload = {**binding(op), "expires_at": expires,
                       "approval_id": approval["id"] if approval else None,
                       "reservation_id": reservation["id"] if reservation else None,
                       "contract_version": op["contract_version"], "policy_version": op["policy_version"]}
            grant = canonical(sign(folder, payload))
        db.execute("UPDATE operations SET status=?, grant=? WHERE id=?", (status, grant, op["id"]))
        db.execute("UPDATE checkpoint SET status=? WHERE id=1", (status,))
        event(db, "authorization." + status, op["id"], now=now, reason=reason,
              policy_version=op["policy_version"], approval_id=approval["id"] if approval else None)
    capture(folder, "Authorization: " + status)
    return status
