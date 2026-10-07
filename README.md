# Agent state and execution contracts

A runnable companion to **The Freedom to Choose the Pieces**: what must an agent system preserve when work is interrupted or its components change?

A service commits a €5 credit. The worker crashes before saving the response. One recovery strategy creates a new operation and credits another €5. The other preserves the original operation identity, finds the existing outcome and finishes with €5 total.

This is a small teaching example using **Python's standard library and SQLite**. It needs no dependencies, API keys, real LLM or payment service. All orders, identities and credits are synthetic.

## Start with the article's controlled comparison

Use Python 3.10 or newer:

```sh
git clone https://github.com/misteliy/agent-state-demo.git
cd agent-state-demo
python3 baseline.py demo
```

The two crash cases use the **same business-service implementation**. Both save the agent's observations and intended action. Only durable recovery saves the operation identity before dispatch.

```text
durable-crash            credits=1 total=EUR 5.00
checkpoint-only-crash    credits=2 total=EUR 10.00
durable-normal          credits=1 total=EUR 5.00
on-time                 credits=0 total=EUR 0.00
no-fee                  credits=0 total=EUR 0.00
```

The baseline deliberately has no one-credit-per-order rule. It deduplicates retries of the same operation; a new identity represents a new intent. Preserving operation identity and prohibiting a second credit are different defenses.

Each invocation creates a fresh directory under `runs/` and prints its location. Existing run directories are never overwritten.

## Then explore the governed extension

```sh
python3 demo.py demo
python3 -m unittest -v
```

The extension adds input validation, scoped authority, expiring approvals, budget reservations, a separate one-credit-per-order rule, service receipts and derived telemetry. The batch includes crash recovery, normal execution, approval, budget denial, wrong-tenant denial, stale data, on-time delivery and no-fee cases. It also runs the original duplicate-credit counterexample for contrast.

The tests exercise real subprocess restarts, changed-payload retries, approval binding and expiry, concurrent budget admission, service deduplication, stale observations, false success responses and telemetry failures. They do not demonstrate concurrent resumption of a task.

## Stop after the effect, then recover

Choose a run-folder name that does not already exist:

```sh
python3 demo.py init runs/my-crash
python3 demo.py run runs/my-crash --crash-at after-credit
# Exit 73 is the intentional process failure.
python3 demo.py inspect runs/my-crash
# The service has committed €5; the runtime lacks confirmation;
# the budget still has €5 reserved.
python3 demo.py run runs/my-crash
# Reconciles the same operation: one credit, €5 spent, nothing reserved.
```

Other crash points are `after-admission`, `after-dispatch` and `after-ack`. Run the commands separately if your shell stops on nonzero exit codes.

## Try approval

```sh
python3 demo.py init runs/my-approval --case approval
python3 demo.py run runs/my-approval
# awaiting_approval
python3 demo.py approve runs/my-approval --as supervisor-a
python3 demo.py run runs/my-approval
# finished; the synthetic €15 credit is confirmed
```

`--as supervisor-a` selects a trusted fixture identity; it is not authentication. Only the batch demonstration automatically supplies this synthetic approval. The ordinary `run` command does not.

## How the state fits together

| Perspective | What it records | Authority |
|---|---|---|
| Task / execution | Objective, progress, pending operation, reservation and status | Runtime records of admitted and unresolved work |
| Agent | Observations, intended action and answer | Working knowledge, which can be incomplete or stale |
| Business | Credit and outcome receipt | What the service committed |

Task and agent records share `runtime.sqlite`. Business records live in `business.sqlite`, representing a separate service boundary. These are overlapping responsibilities, not a rule that every system needs three databases. A copied receipt informs the agent; it does not give the agent authority to change the service's outcome.

The selector is a transparent rule-based stand-in for a model. Late deliveries, on-time deliveries and orders without a fee follow different paths. Recovery resolves pending work before asking the selector for its next action.

## Where declarations end and implementation begins

[contracts.json](contracts.json) declares tool inputs and names behavioral obligations. [policy.json](policy.json) supplies authority and budget settings. Python interprets the supported input-schema fields and policy limits. The service and recovery code explicitly implement eligibility checks, atomic commits and reconciliation.

Adding a postcondition string does not implement its guarantee. This is not a compiler for arbitrary contracts, and the complete trajectory is not made deterministic by writing JSON.

## What is implemented—and what is still a proposal

Implemented under local assumptions:

- Persist the exact logical operation before dispatch and reconcile it after a lost response.
- Commit a credit and its deduplication receipt in one service transaction. Reusing an identity with changed arguments is rejected.
- Bind approval and the signed execution grant to the operation, principal, tenant, arguments and contract/policy snapshots.
- Reserve budget before dispatch. Confirmed effects settle it; definitive no-effect rejections release it; unknown outcomes retain it.
- Keep recovery independent of observer snapshots and trace export.

**Not implemented:** an actual LLM, model replacement during a task, cross-harness handoff, ownership fencing, concurrent task resumption or production authentication. Those are proposed next experiments in the article. A shared ownership protocol cannot be inferred from this single-worker demo.

The service contract is essential. A wrapper around an arbitrary API cannot promise the same effect guarantees without a trustworthy deduplication or reconciliation mechanism. A missing response does not prove nothing happened.

## Files and evidence

| File | Responsibility |
|---|---|
| [baseline.py](baseline.py) | Same-service €10 versus €5 recovery comparison |
| [demo.py](demo.py) | Extended selector, intent persistence, dispatch, recovery and CLI |
| [contracts.py](contracts.py), [contracts.json](contracts.json) | Supported input schemas and declared obligations |
| [authority.py](authority.py), [policy.json](policy.json) | Scope checks, approvals, grants and reservations |
| [service.py](service.py) | Authoritative business transactions and receipts |
| [store.py](store.py) | Runtime records, durable events and observer snapshots |
| [telemetry.py](telemetry.py) | Derived JSONL and OTLP JSON trace exports |
| [test_baseline.py](test_baseline.py), [test_demo.py](test_demo.py) | Behavioral tests |

Generated run folders contain SQLite databases, snapshots and telemetry. The extended batch writes `results.json` for its eight governed cases and `playback.json` including the baseline counterexample. Rebuild an extended run's exports with `python3 demo.py export <run-folder>`. Baseline and extended run schemas differ; use the matching program to inspect or resume each run.

Generated state and signing keys are ignored by Git. The repository contains synthetic fixtures and source code, not saved operational data.

See [architecture and limits](ARCHITECTURE.md) for approval expiry, budget scope, receipt retention and failure assumptions.
