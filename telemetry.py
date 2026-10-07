"""OTLP/HTTP JSON body export. No SDK/collector. Custom fields use demo.*.

Exports are derived views; recovery never reads them. This is an educational
encoder, not a claim of certified interoperability or standardized governance.
"""
import hashlib
import json
import sys
from pathlib import Path
from store import checkpoint, config, database


def attributes(values):
    result = []
    for key, value in values.items():
        if value is None:
            continue
        item = ({"boolValue": value} if type(value) is bool else
                {"intValue": str(value)} if type(value) is int else {"stringValue": str(value)})
        result.append({"key": key, "value": item})
    return result


def short_id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def export(folder):
    folder = Path(folder)
    with database(folder / "runtime.sqlite") as db:
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]
    cfg, current = config(folder), checkpoint(folder)
    for row in events:
        row["attributes"] = json.loads(row["attributes"])
        row.update(run_id=cfg["run_id"], trace_id=cfg["trace_id"])
    (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    if not events:
        return
    # This closed snapshot span does NOT claim the agent completed.
    span_id = short_id(cfg["run_id"] + ":snapshot:" + str(events[-1]["id"]))
    start, end = min(e["time_ns"] for e in events), max(e["time_ns"] for e in events)
    otel_events = []
    for e in events:
        values = {"demo.operation.id": e["operation_id"], "demo.attempt.id": e["attempt_id"],
                  "demo.event.sequence": e["id"], **{"demo." + k: v for k, v in e["attributes"].items()}}
        otel_events.append({"timeUnixNano": str(e["time_ns"]), "name": e["kind"], "attributes": attributes(values)})
    root = {"traceId": cfg["trace_id"], "spanId": span_id, "name": "demo.execution.snapshot", "kind": 1,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start + 1, end)),
            "attributes": attributes({"demo.run.id": cfg["run_id"], "demo.run.status": current["status"],
                                      "demo.snapshot": True, "gen_ai.agent.name": "delivery-credit-demo"}),
            "events": otel_events, "status": {"code": 0}}
    spans = [root]
    starts = {e["attempt_id"]: e for e in events if e["kind"] == "tool.dispatched"}
    for e in events:
        if e["kind"] != "tool.response" or e["attempt_id"] not in starts:
            continue  # Never fabricate completed spans for unacknowledged calls.
        first = starts[e["attempt_id"]]
        tool = first["attributes"]["tool"]
        spans.append({"traceId": cfg["trace_id"], "spanId": short_id(e["attempt_id"] + ":" + span_id), "parentSpanId": span_id,
                      "name": "execute_tool " + tool, "kind": 1,
                      "startTimeUnixNano": str(first["time_ns"]),
                      "endTimeUnixNano": str(max(first["time_ns"] + 1, e["time_ns"])),
                      "attributes": attributes({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                                                "demo.operation.id": e["operation_id"], "demo.attempt.id": e["attempt_id"],
                                                "demo.effect.status": e["attributes"].get("effect_status", "read")}),
                      "status": {"code": 0}})
    body = {"resourceSpans": [{"resource": {"attributes": attributes({"service.name": "agent-state-demo"})},
                              "scopeSpans": [{"scope": {"name": "demo.execution", "version": "2.0"}, "spans": spans}]}]}
    (folder / "trace.otlp.json").write_text(json.dumps(body, indent=2))


def safe_export(folder):
    try:
        export(folder)
    except OSError as error:
        print(f"Optional telemetry export failed: {error}", file=sys.stderr)
