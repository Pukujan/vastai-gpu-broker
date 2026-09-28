# Architecture and safety boundaries

## Initial shape

- **Agent-facing interface:** local CLI in the repository. Agents clone/read the instructions and call the same commands; no internet-facing control endpoint is required.
- **Current live function:** refresh Vast's offer API and retain the original records with a capture timestamp. GitHub Actions can refresh the cache every six hours, but schedules may be delayed; they are convenience snapshots only.
- **Selection:** an agent resolves an exact model set using official Hugging Face sources, applies [`GPU_SELECTION_POLICY.md`](../GPU_SELECTION_POLICY.md), and performs a fresh Vast offer search. The synced JSON is not an authoritative quote and cannot authorize a purchase.
- **Data:** the committed JSON cache is a published snapshot, not a database. No database is needed before coordinating multiple running leases. Do not commit model credentials or Vast keys.

## Why not a hosted web app yet

A web control plane needs to hold a paid-capable credential, authenticate callers, enforce per-user caps, prevent concurrent duplicate leases, store active lease state, and clean up after failures. That adds security and billing failure modes before there is a need for shared coordination. A local/agent-invoked CLI can keep the launch key on the user's machine. If multiple independent agents later need shared leases, add a small authenticated service plus durable lease database and a separate janitor, with idempotent request IDs.

## Required future lease lifecycle

Before enabling any paid create command, implementation must:

1. Require an exact model profile with official Hugging Face URLs, all models loaded simultaneously, and explicit user hourly and total spend/runtime caps.
2. Re-search live offers, re-check the chosen offer immediately before launch, and display GPU + storage + total hourly price and bandwidth rates.
3. Require explicit request confirmation for the selected offer and cap; use a scoped instance-management key only.
4. Persist the returned instance ID, selected offer, quote, cap, start time, and absolute deadline immediately. Apply a short bounded launch/status poll; never loop forever while the disk is billable.
5. Have an independent watchdog attempt destroy by deadline; destroy (not stop) on normal completion, cancellation, failed setup, or deadline. Verify with the instance API and reconcile outstanding IDs after restart.
6. Report teardown failures prominently and keep retrying/backing off. A remote API/network outage or dead control machine means cleanup cannot be guaranteed; never describe a client watchdog as an absolute provider-side cutoff.

Vast docs currently do not document a rental API maximum-duration field that guarantees termination. The offer `duration` query is only a minimum available time. Until crash/network recovery and bounded spend behavior are implemented and reviewed, this repo must remain read-only.

## Serverless decision

Vast Serverless may make sense for a stable always-addressable endpoint that receives repeated traffic and benefits from worker auto-scaling. It is a poor fit for the initial variable-model, temporary-hosting flow: Loading/Ready incur GPU charges, Inactive still incurs storage and bandwidth, and stopping an endpoint is not the same as destroying it. If evaluated later, set model-specific `gpu_ram` explicitly from the model/runtime's official instructions, never rely on Vast's documented default.
