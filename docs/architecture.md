# Architecture entry point

The implementation target is a portable Python module and CLI sharing one research, pricing and lease-control engine. An agent supplies a model-hosting task; the router returns the next required research/input/action and blocks paid transitions until its artifacts and limits are valid.

[SPEC.md](../SPEC.md) is the product contract. Read [PDD.md](PDD.md), [SDD.md](SDD.md) and [TDD.md](TDD.md) for properties, interfaces/state transitions, and acceptance evidence. [GPU_SELECTION_POLICY.md](../GPU_SELECTION_POLICY.md) defines model evidence and selection semantics. [checkpoint-policy.md](checkpoint-policy.md) defines authoritative progression and adoption.

## Current status

The Python broker, marketplace client, research and budget gates, pinned Open-Jev runner, durable lease journal, local supervisor, and remote recovery service adapters are implemented. Paid use remains blocked until two remote paths and their shared registry are deployed and tested in separate failure domains. Consult the current continuity/checkpoint projection for actual evidence; this architecture does not itself enable paid actions.

## Component boundaries

1. **Research router** emits exact-model discovery and source-backed deployment tasks, validates evidence identity/provenance, and keeps unknowns visible. Ambiguous requests produce variant/quantization candidates and live price comparisons before exact-model confirmation.
2. **Marketplace client and planner** retrieves current offers across verification/rental statuses, validates compatible/authorized candidates, compares complete known costs and relevant measured performance, and binds a proposal to fresh evidence/quote/caps.
3. **Lease controller** enforces the paid authorization, finite bidding/start deadlines, request locking, durable ownership and bounded installer/inference recipe.
4. **Independent cleanup supervisors** read the durable fenced lease registry and meaningful workload activity, enforce cold/idle/hard deadlines, destroy at completion/failure/deadline, verify provider absence, and reconcile obligations after restart. Two paths in separate control-host failure domains must be armed before create.
5. **Adopter interface** exposes the same operations as CLI and importable Python API. A future MCP/dashboard is a wrapper around that core.

GitHub owns versioned code, contracts and accepted progression. Offer snapshots are timestamped caches; they never authorize a rental. The local ignored state directory owns source captures, task artifacts and active lease obligations. Private holdout fixtures live outside the public repository and builder context.

## Hosting and billing

A standalone local module avoids operating an extra web control plane for initial adoption. Multiple remote agents can later use an authenticated broker with shared durable lease state and an independent janitor. The transport does not replace core policy enforcement.

Temporary GPU hosting ends with destruction, followed by provider verification. Stopping/pausing retains storage charges. An explicitly configured stop stage may retain disk briefly before the independent destroy deadline; never retain it indefinitely as cleanup. A local process cannot guarantee remote cleanup during controller/network failure, so unresolved destruction remains a visible retryable failure. A logged-in browser session can assist interactive work but cannot count as a cleanup path. Do not claim a hard provider-side cutoff that the documented API does not offer.

Vast Serverless can be evaluated for recurring traffic and stable endpoints later. Its inactive storage charges and worker lifecycle are not the initial ad hoc model-testing workflow. [Official docs router](official-docs.md) contains the source links governing provider behavior.
