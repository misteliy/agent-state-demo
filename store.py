"""Local durable records. Agent values and execution state share one database."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def clock(now=None):
    return time.time() if now is None else now


def config(folder):
    return json.loads((Path(folder) / "config.json").read_text())


def document(folder, name):
    return json.loads((Path(folder) / name).read_text())


def event(db, kind, operation_id=None, attempt_id=None, now=None, **attributes):
    db.execute("INSERT INTO events(time_ns, kind, operation_id, attempt_id, attributes) VALUES (?, ?, ?, ?, ?)",
               (int(clock(now) * 1_000_000_000), kind, operation_id, attempt_id, json.dumps(attributes)))


def checkpoint(folder):
    with database(Path(folder) / "runtime.sqlite") as db:
        row = dict(db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone())
    row["agent"] = json.loads(row["agent"])
    row["finished"] = bool(row["finished"])
    return row


def operation(folder, operation_id):
    with database(Path(folder) / "runtime.sqlite") as db:
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown operation")
    return dict(row)


def snapshot(folder, label="Current durable state"):
    folder = Path(folder)
    current = checkpoint(folder)
    with database(folder / "runtime.sqlite") as db:
        operations = [dict(x) for x in db.execute("SELECT * FROM operations ORDER BY rowid")]
        for op in operations:
            op.pop("grant", None)
        approvals = [dict(x) for x in db.execute("SELECT * FROM approvals ORDER BY rowid")]
        reservations = [dict(x) for x in db.execute("SELECT * FROM reservations ORDER BY rowid")]
        events = [dict(x) for x in db.execute("SELECT * FROM events ORDER BY id")]
    with database(folder / "business.sqlite") as db:
        credits = [dict(x) for x in db.execute("SELECT * FROM credits ORDER BY rowid")]
    return {"label": label, "agent": current["agent"],
            "runtime": {"status": current["status"], "finished": current["finished"],
                        "pending_operation": current["pending_operation"], "operations": operations},
            "authority": {"approvals": approvals, "reservations": reservations,
                          "reserved_cents": sum(r["amount_cents"] for r in reservations if r["status"] == "reserved"),
                          "spent_cents": sum(r["amount_cents"] for r in reservations if r["status"] == "spent"),
                          "limit_cents": document(folder, "policy.json")["daily_credit_budget_cents"]},
            "business": {"credits": credits, "total_credit_cents": sum(c["amount_cents"] for c in credits)},
            "events": events}


def capture(folder, label):
    """Observer-only playback, never an input to recovery."""
    try:
        value = snapshot(folder, label)
        with (Path(folder) / "snapshots.jsonl").open("a") as out:
            out.write(json.dumps(value) + "\n")
    except OSError:
        pass  # Optional playback failure must not change execution semantics.
