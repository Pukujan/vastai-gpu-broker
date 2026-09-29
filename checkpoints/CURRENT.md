# Current checkpoint

<!-- continuity:current {"active_task":"VBR-0001","active_task_file":"tasks/TASK-VBR-0001-research-first-ephemeral-runner.md","protocol_version":"0.1.0-draft","schema":"project-continuity.current.v1"} -->

Updated: 2026-09-29. Contract target: SPEC/PDD/SDD/TDD version 1; implementation schema is pending.

Source authority: `https://github.com/Pukujan/vastai-gpu-broker` (visibility verified public). PR #5 was squash-merged at `0188f251d63186c83d34249a4ab29e90c19e71b8`; PR #7 was squash-merged into `main` at `bf1710536b663be0a155396504595371d4198f03` on 2026-09-29. PR #7 applies the owner's all-in hourly cap to compute, startup and network exposure. The merge does not claim that paid-run acceptance is complete.

## Content system pin

- Helper source: [`Pukujan/content-generation-modules`](https://github.com/Pukujan/content-generation-modules) at version `0.5.7`, exact commit `c069613ca8b3e02bcf5aba1960160583537f8a3a`.
- The full eight-module pin and adapter are recorded in `.content-system/`; helper implementation/history is checked out at that revision for validation and is not vendored.
- `AGENTS.md` carries the helper's exact always-on writing block. `scripts/validate_cgm_adoption.py` checks the pin, boot block, image source record, and asset digest; CI also runs the pinned helper's adapter and HSW checks.
- The adoption overlay and tests are merged on `main` through PR #5. This pin does not imply that every planned broker feature is implemented or that a paid challenge passed.

Canonical task: [VBR-0001 / issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1). This file is a projection; the continuity overlay may preserve its content in the target's canonical checkpoint layout.

## Actual capability

- The working tree now contains a Python broker package with exact Hugging Face candidate resolution, evidence validation, live Vast offer retrieval/normalization, cost comparison, bounded offer caching, a review/authorization gate, provider adapters, a lease journal/supervisor, and a timestamped snapshot workflow that proposes a PR rather than pushing snapshots to `main`. PR #5 includes a fail-closed guardian/registry interface and independent instance/volume cleanup verification.
- Hourly offer eligibility and execution-plan validation now include the bounded network allowance and startup exposure with the machine rate; A57 and M48 document and test this rule. This closes the cost-accounting gap for the owner's $0.20/hour all-in cap.
- The supported command path is still read-only (`offers` and `route`); the README correctly says there is no supported end-to-end command that researches, prepares, confirms, creates, installs, probes, and cleans up a real model run.
- Joint fit for multiple simultaneously resident models and the parallel bid-race coordinator are explicitly blocked with zero creates. These remain required future capabilities for the full user goal, not completed features.
- The plan and contracts specify complete cost accounting, model evidence, lifecycle cleanup, multi-model fit, and finite aggregate race limits. Unsupported provider units, unknown costs, stale/cached quotes and missing spend limits fail closed.

## Changed requirements

- Require executable next-action routing, rather than relying on an agent remembering instructions.
- Permit researched external tested deployment configurations when publisher hardware numbers are missing, preserving provenance and unknowns.
- Separate a source-tested tier from a proven minimum and from a locally measured fit.
- Define finite interruptible bidding, network-aware cost comparison, ambiguous-create recovery, and verified destruction.
- Require two cleanup paths in separate control-host failure domains, a durable shared lease registry, and failure tests for lost guardians, registry outages, and browser-session/GitHub Actions substitutes.
- Distinguish GPU start urgency from inference-performance targets; bind the final `run it` authorization to the exact quote/plan, and deliver benchmark results before a separate cleanup receipt.
- Include startup and network allowance in the owner's hourly affordability check at both route and run-plan validation boundaries.
- Make this repository the versioned authority with adopter and development checkpoints.
- Define private holdout isolation and distinguish the known-model low-context challenge from unseen-model evaluation.

## Validation evidence

- At helper revision `c069613ca8b3e02bcf5aba1960160583537f8a3a`, the adopter validator, full CGM adapter validator, and HSW automation check pass.
- `python -m pytest -q tests/test_router.py`: 15 passed. `python -m pytest -q`: 163 passed on PR head `631d687e8f22cdd4da6697e1116fbb23e45b79b2`, merged as `bf1710536b663be0a155396504595371d4198f03`; `git diff --check` passed. GitHub `gates` passed on PR #7 in [run 36516485379](https://github.com/Pukujan/vastai-gpu-broker/actions/runs/36516485379).
- The publisher's model card identifies Open-Jev 9B as a LoRA adapter and scalar decision head that requires Qwen3.5-9B at pinned revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`. The upstream [consumer-GPU report](https://github.com/Zefan-Cai/Open-Jev/blob/3308a15ccd7eea1df7a37d6ddc39b023b801ba16/docs/consumer-gpu.md) reports a single RTX 5060 Ti 16 GB BF16 test. This is source-reported compatibility evidence, not a local run or a proven minimum.
- Local cleanup now preserves the instance while volume inventory is unavailable and ownership is not yet known, then retries discovery; if that discovery anchor disappears, it keeps the orphan-volume obligation pending. Unresolved-volume-only records are reconciled without deleting uncertain resources. The local worker records heartbeat/attempt/error metadata; an in-host launcher restarts an exited child or one with a stale heartbeat (90 seconds) or stalled tick (one hour), after terminating it. These are local recovery improvements, not independent guardian coverage.
- Fake-only public lifecycle tests now exercise fail-closed guardian readiness, controller and local worker loss, separate guardian/client objects, provider and shared-registry outages, recovery after restore, and idempotent cleanup. The registry snapshot carries the durable attempt label and pre-create resource baseline; tests cover discovery after an unpublished ambiguous create, baseline exclusion, publication before destroy, malformed post-cleanup listings, and pending recovery on listing failures. These fakes do not establish deployment, physical failure-domain separation, or real service behavior.
- On Windows, journal byte-lock acquisition now retries `EACCES` contention beyond the CRT's ten-attempt limit and propagates other errors; the regression test exercises more than ten consecutive contention responses.
- The pinned CGM adoption, adapter, and HSW contract checks passed; `continuity validate --root .` returned `VALID`. A54–A56 and M46–M47 remain release gates; the local fake-based tests do not establish deployed guardian recovery or real service behavior.
- GitHub reports `allow_auto_merge=true`. The only protected branch pattern is `main`; no active repository rulesets were found. `main` requires the strict `gates` check, has `required_approving_review_count=0`, enforces branch protection for admins, and blocks force-pushes and branch deletion. PR #5 was ready and had squash auto-merge armed; GitHub merged it at 2026-09-29 01:50:40 UTC. `gates` passed on PR head `8b5589a04dfa9e9989b0219c1a387a4cccb01bab` in run https://github.com/Pukujan/vastai-gpu-broker/actions/runs/36506363979 and on merge commit `0188f251d63186c83d34249a4ab29e90c19e71b8` in run https://github.com/Pukujan/vastai-gpu-broker/actions/runs/36509801481.
- No paid instance or create API call was made; no Open-Jev inference or post-destroy inventory verification has completed. A previous read-only offer query found a candidate, but that quote is stale and is not create authorization.

## Blockers and open inputs

- Owner clarified that `$0.20/hour` is the all-in cap including network. A finite maximum total, runtime/start deadline, and bounded network allowance for the one run are not yet recorded; bids remain unauthorized. Missing values must block create.
- A dedicated scoped create/manage/destroy credential and production recovery-service credentials are not confirmed in the local configuration. No external guardian service or shared lease registry has been deployed.
- Full end-to-end CLI runner, prepared install/inference harness, lease lifecycle real-provider validation, failure injection, and private evaluator remain pending.
- Joint simultaneous-model placement/acceptance and parallel race coordination remain unsupported; do not imply that local test passes establish these modes.
- Repository auto-merge remains enabled. PR #7 merged by squash after `gates` passed; the `main` rule requires no approving review. Future PRs still need the strict `gates` check. This meets the owner's single-worker workflow; no second human reviewer is configured or required.
- The CGM overlay, implementation, docs, tests, and all-in hourly cap fix are merged through PR #7. This checkpoint does not claim a paid-run release or acceptance.
- Vast documents stop/destruction behavior around zero credits, but permits negative-balance grace and ongoing stopped-disk charges without a fixed deletion deadline. This is billing context only; the broker's cleanup must actively destroy and verify the lease.
- The intended lifecycle is automatic destroy after completion/failure/cancellation/authorized idle deadline. The new local interface requires two declared independent-service guardians, a durable fenced registry readback, and publication of instance/volume IDs before access; physical failure-domain separation is deliberately not inferred from declarations. No external guardian or shared registry backend is provisioned, and A54–A56 outage/recovery tests against deployed services remain open. The local child still cannot guarantee provider deletion during total host/network/API failure; its in-host monitor also ends when the launcher process exits. Jev's local Chrome session and GitHub scheduled workflows do not satisfy guardian readiness. P42/P44/P45 and A50–A56 are not passed. Account-credit exhaustion is not the cleanup plan. Do not treat paid-capable Python APIs as a supported real-money workflow.
- A session should rent one GPU for the requested model/benchmark bundle, switch or co-reside models according to the explicit workload mode, then destroy the ephemeral instance. Keeping an on-demand GPU permanently rented only makes sense after measured utilization and current offers justify its idle-hour cost.

## Next action

Continue the same Open-Jev 9B live-test acceptance path. Resolve the scoped Vast and remote-cleanup credentials without exposing values; implement/deploy the shared registry and two separated recovery paths; confirm the remaining finite total/runtime/network limits; refresh the 5060 Ti quote; then create, run one typed Open-Jev inference, destroy, and verify fresh complete Vast inventory. No paid create is permitted until the cleanup gates and explicit caps pass. Keep `gates` required and verify the merge SHA for each auto-merged PR.
