# Vast.ai GPU broker

This repository is public. Treat all committed files, issues, workflow logs, and offer snapshots as public; never place credentials, account data, or private lease state here.

This repository contains a Python package for evidence-first model hosting decisions and temporary Vast.ai GPU leases. It can query current offers, validate supplied model evidence, compare costs under explicit limits, and includes a lease controller with a durable local journal and an independent cleanup supervisor.

**A paid model run is not ready for adoption.** The CLI currently exposes only the read-only `offers` and `route` commands. There is no end-to-end CLI command that gathers evidence, builds and reviews the deployment recipe, refreshes and confirms the proposal, then invokes a trusted install/inference operation. Python APIs can create instances if called through the intended authorization path, and low-level provider/controller methods can also be called directly. Treat those paid-capable APIs as implementation components, not as a supported, safe model-running workflow. No paid model install or real Vast inference/cleanup challenge is claimed as tested.

## Install and adopt

Requirements: Python 3.11 or newer. The package declares no runtime third-party dependencies.

```powershell
python -m pip install -e .
vast-broker --help
```

Or invoke the same CLI without installing its command entry point:

```powershell
python -m vast_broker --help
```

For another project, pin this repository to a reviewed commit and follow [checkpoint and adoption policy](docs/checkpoint-policy.md). Record the source URL and commit, contract version, entry point, owner policy provenance, and the ignored local location used for active lease journals. Read [SPEC.md](SPEC.md), [SDD.md](docs/SDD.md), [GPU selection policy](GPU_SELECTION_POLICY.md), [official provider docs](docs/official-docs.md), and the [current checkpoint](checkpoints/CURRENT.md) before use. Do not treat a copied offer snapshot or an agent-supplied note as authorization.

## Refresh current offers

`offers` performs a live, read-only query at invocation time. Configure `VAST_API_KEY` through the local secret store or process environment using a read-only key with the Vast `misc` permission. Never put the value in command arguments, request files, logs, or the repository.

```powershell
vast-broker offers --disk-gb 100 --output current-offers.json
```

`--disk-gb` should match the temporary disk quantity you intend to compare. The command queries `ondemand`, `bid`, and `reserved` rental types, retains verification and availability statuses, and writes normalized quotes, capture time, and completeness information. If output reaches the API limit or pagination is incomplete, the result is marked incomplete; it is not evidence of the global cheapest offer. Reserved rows are comparison-only and cannot go through the direct create path.

The older raw snapshot synchronizer is also available:

```powershell
python scripts/sync_offers.py
```

It updates `data/latest.json` as a sanitized convenience cache. The scheduled [refresh workflow](docs/offer-refresh.md) opens or updates a pull request; schedule timing is not a launch-time refresh. Neither that file nor a scheduled run proves an offer is still available. Search again immediately before any future paid action.

## Evidence-first routing

The router needs an exact artifact identity, the workload, captured source-backed evidence, a fresh normalized offer result, explicit limits, and the appropriate selection/payment authorization. The `route` command validates supplied files; it does not automatically research the web or fabricate missing evidence.

```powershell
vast-broker route `
  --request request.json `
  --evidence evidence-by-candidate.json `
  --market current-offers.json `
  --limits owner-limits.json `
  --output route-result.json
```

`request.json` supplies the model/artifact request, workload, request ID, and any exact or scoped selection authorization. `evidence-by-candidate.json` maps the resolved candidate key to retrieved source captures and claims. Source text digests and cited excerpts are checked, but this is not automated scientific review: semantic truth and source relevance still need human/agent review. If publisher hardware requirements are absent, the route requires relevant tested deployment evidence or preserves fit as unknown. Estimates and file sizes alone do not prove a model fits.

For an ambiguous family or base-only request, research and compare exact candidates before asking for artifact selection. An exact repository plus requested files proceeds for that artifact without a broad alternatives menu. An explicit `scoped_auto_select` authorization can choose only within its stated base/format/quantization/provider scope and objective; it does not authorize scope expansion. See [artifact resolution](SPEC.md#artifact-resolution-and-scoped-selection-authorization).

`owner-limits.json` must provide finite concrete hourly, total, runtime, network, disk, start-deadline, cold-start, idle, and hung-request limits. Bid routes also need a per-machine-hour bid cap, increment, and attempt count. The router does not infer missing caps from account balance or credentials. The offer quote, chosen artifact, evidence, recipe, and limits must remain bound together; changes require fresh planning and, when needed, reconfirmation.

## Read-only and paid-capable entry points

| Entry point | Effect | Current use |
| --- | --- | --- |
| `vast-broker offers` / `search_offers()` | Read-only live marketplace search | Available; requires configured Vast read access. |
| `vast-broker route` / `route_request()` | Read-only evidence, selection, quote, and limit routing | Available; returns a next action/proposal or a blocker. It does not create an instance. |
| `authorize_run()` | Calls `LeaseController.run()` after checking a confirmed proposal against the supplied plan | Paid-capable Python API; not wired to a supported end-user CLI workflow. It requires a confirmed deployment recipe and exact plan bindings. |
| `LeaseController.run()` / provider `create_instance()` | Creates/manages a Vast instance | Low-level paid-capable APIs. Direct use can bypass router authorization; do not call them directly for an agent task. |

`READY_TO_RUN` is a routing/proposal result, not evidence that an instance was created or a model was installed. The intended Python paid transition is `authorize_run`; it calls the lease controller only after checking the proposal digest, freshness, offer ID/type, deadlines, bid and spend terms, disk/network allowance, artifact identity, recipe digest, image, and exact create parameters. The caller remains responsible for rebuilding `current_proposal` from a genuinely fresh offer/evidence query and supplying a trusted, bounded operation. There is no packaged generic model installer or inference probe; do not execute commands copied from model cards or offer descriptions.

## Lease ownership and cleanup

The lease controller writes a request-scoped journal before create, starts a separate supervisor process, records ownership, and avoids retrying an ambiguous create until it reconciles the account instance list by its unique label. The intended behavior is automatic destruction after completion, failure, cancellation, or the authorized idle/deadline policy. The agent is not the sole cleanup owner: an independent out-of-host guardian must detect agent/controller/watchdog loss, destroy owned resources, and verify provider-side absence. The worker now journals tick attempts, sanitized error types, and an independent heartbeat; the in-host launcher watches process exit, stale heartbeat (90 seconds by default), and stalled tick progress (one hour by default), terminates a stale child before restarting it, and records restart state. That monitor exists only while the launcher process remains alive and shares the initiating control host. It does not satisfy the external guardian requirement or cover launcher-host loss. Deployed guardians and failure-injection evidence are still required before this can be treated as a supported paid workflow.

The controller attempts destruction on completion and failure, then checks a successful complete provider listing for absence. Unavailable or incomplete listings, ambiguous create outcomes, and failed destruction remain unresolved journal obligations for recovery; stop/pause is not equivalent to destruction.

An adopter wiring the paid API must configure an independent provider factory (`VAST_BROKER_PROVIDER_FACTORY=module:callable`), a scoped lease credential, and a private, durable `VAST_BROKER_JOURNAL_DIR` before create. The supervisor inherits its configured environment and needs to be able to run independently of the main operation. The journal is operational state, not a repository checkpoint; keep it out of version control and reconcile it with the code version that understands its schema.

Deadlines and cost calculations are local controls and estimates. Vast does not document a per-lease maximum lifetime or per-task spend/network cap. The broker must actively destroy a lease after use; it does not wait for account balance depletion. Vast's zero-credit behavior is only a billing fact: disk charges can continue, balances can go negative during a grace buffer, and saved-card charging may apply. A controller, host, or network outage can still prevent prompt cleanup; only fresh provider-confirmed absence establishes that an instance was removed. See [billing and cleanup behavior](docs/official-docs.md#cost-and-instance-lifecycle), [architecture](docs/architecture.md), and the [lease supervisor failure requirements](docs/SDD.md#state-machine-and-durable-lease-ownership).

## Current blockers and evidence

- There is no supported, complete paid-run command or built-in model-specific installation/inference recipe.
- The CLI `route` command consumes supplied request, evidence, market, and limit files; automatic research capture and an end-to-end fresh-plan-and-confirm loop are not provided.
- The Python lease/provider APIs can create resources outside `authorize_run` when directly invoked, so safe use depends on the adopter calling the router gate first.
- Existing automated lifecycle tests use fakes. No paid model install, real inference, provider spend cap, or real Vast cleanup challenge is claimed as tested.
- Local deadlines and cleanup retries cannot guarantee provider action during total controller/network/provider failure, and quotes do not guarantee future availability or final charges.
- The checkpoint is the source of current acceptance and release status. If it disagrees with the code state, resolve and update that checkpoint through the repository's reviewed progression before claiming readiness.

See [architecture](docs/architecture.md), [PDD](docs/PDD.md), [TDD](docs/TDD.md), [agent credential access](docs/agent-credential-access.md), and the [offer refresh workflow](docs/offer-refresh.md) for implementation boundaries and security details.
