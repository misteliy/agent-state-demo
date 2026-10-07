# Architecture and limits

## The execution boundary

```text
choose from current observations
  -> validate inputs; persist exact intent and operation identity
  -> check authority; bind approval; reserve capacity atomically
  -> dispatch with a scoped, expiring execution grant
  -> service checks the grant and current business preconditions
  -> service commits the effect and outcome receipt together
  -> runtime matches the response against the service outcome
  -> settle budget and checkpoint the observation atomically
  -> choose again
```

Each service has its own SQLite transaction. There is no distributed transaction across the runtime and business databases. The synchronous local service call simulates the boundary; it does not simulate every possible network failure.

## Unknown outcomes

After a crash, a dispatched credit can exist in the service while the runtime has no confirmed result. Recovery preserves the original identity and arguments and asks for that operation's outcome. A missing receipt permits retry here only because the service atomically deduplicates that identity. An arbitrary remote service may still commit a previously dispatched request.

A definitive service rejection is persisted as a no-effect outcome, or tombstone. Later attempts with that operation cannot create an effect. This allows the runtime to release the reservation. A generic exception or timeout does not establish no effect, so capacity remains reserved.

Receipts and rejection tombstones are retained for the database's lifetime. There is no production retention or deletion protocol.

## Authority and budgets

Approvals bind the principal, tenant, operation identity, tool, argument digest, contract digest and policy digest. Retrying the same operation can reuse that binding; a new intent cannot borrow it. Contract and policy files are copied into each run and content-hashed. Migration of those snapshots is not implemented.

Grants are HMAC-signed with a locally generated key. Identities, files and that key are trusted fixtures. This demonstrates binding, not authentication, credential isolation or an adversarial sandbox.

The service checks expiry inside the transaction admitting a new effect. This is not a hard real-time promise about the wall-clock moment of commit. After a committed effect's approval expires, its historical receipt can still be reconciled. A retry that might create a new effect needs current authorization. There is no immediate revocation endpoint.

Budget admission uses `BEGIN IMMEDIATE` to serialize checks and reservations. Capacity is tenant-scoped within a runtime database and charged to the UTC day of first reservation. A retry on a later day retains that allocation. This is admission-day accounting, not a cap on all credits committed during a calendar day, and not an organization-wide budget across demo directories.

The ledger tracks reserved and settled capacity; confirmed spending comes from service receipts. The business service remains authoritative for the credit itself. Token costs, time limits and call quotas need separate accounting.

## Observability

Durable events correlate operations, attempts, approval, reservation, dispatch, verification and settlement. `events.jsonl`, `trace.otlp.json` and observer snapshots are derived views. Recovery never reads them.

The exporter emits an OTLP/HTTP JSON body with execution-snapshot and completed tool spans. It does not invent model-inference spans or completed spans for missing responses. Custom `demo.*` attributes are illustrative, not a standardized governance vocabulary. Exports are inspection snapshots, not an incremental telemetry stream. Tests check structure and correlation; they do not certify compatibility with every backend.

## Explicit limits

- One worker owns a run. Concurrent budget admissions and same-operation service calls are tested; concurrent task resumption is unsupported. There are no leases or fencing tokens.
- Local SQLite, injected process exits and synthetic orders do not establish network-partition, power-loss or Byzantine-failure guarantees.
- No cancellation, rollback, compensation, delegation chain or policy migration. A rejected operation cannot later succeed under the same identity.
- No arbitrary postcondition compiler or general output-schema validator. Behavioral conditions are implemented explicitly. A check cannot undo an already committed effect.
- The selector keeps one observation per tool for one order. It is predefined and uses no model. No model-continuation evaluation or cross-harness transfer has been run.
- The one-credit-per-order policy is illustrative; actual business policy may require escalation or another remedy for new harm.

The example uses its own Python policy evaluator. It does not implement Dogwood, Temporal or LangGraph, and it is not a production agent runtime or a proposed universal standard.
