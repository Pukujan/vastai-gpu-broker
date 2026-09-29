# Current checkpoint

<!-- continuity:current {"active_task":"VBR-0001","active_task_file":"tasks/TASK-VBR-0001-research-first-ephemeral-runner.md","protocol_version":"0.1.0-draft","schema":"project-continuity.current.v1"} -->

Updated: 2026-09-28. Contract target: SPEC/PDD/SDD/TDD version 1; implementation schema is pending.

Source authority: `https://github.com/Pukujan/vastai-gpu-broker` (visibility verified public). Working branch: `feature/research-first-ephemeral-router`. PR #5 is open as a draft and includes the guardian interface and verified volume cleanup. Issue #4 records the two-guardian owner requirement. The branch is not merged; this checkpoint does not claim a release.

## Content system pin

- Helper source: [`Pukujan/content-generation-modules`](https://github.com/Pukujan/content-generation-modules) at version `0.5.7`, exact commit `c069613ca8b3e02bcf5aba1960160583537f8a3a`.
- The full eight-module pin and adapter are recorded in `.content-system/`; helper implementation/history is checked out at that revision for validation and is not vendored.
- `AGENTS.md` carries the helper's exact always-on writing block. `scripts/validate_cgm_adoption.py` checks the pin, boot block, image source record, and asset digest; CI also runs the pinned helper's adapter and HSW checks.
- The adoption overlay and tests are committed on the draft PR branch, not merged. This pin does not imply that every planned broker feature is implemented or that a paid challenge passed.

Canonical task: [VBR-0001 / issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1). This file is a projection; the continuity overlay may preserve its content in the target's canonical checkpoint layout.

## Actual capability

- The working tree now contains a Python broker package with exact Hugging Face candidate resolution, evidence validation, live Vast offer retrieval/normalization, cost comparison, bounded offer caching, a review/authorization gate, provider adapters, a lease journal/supervisor, and a timestamped snapshot workflow that proposes a PR rather than pushing snapshots to `main`. PR #5 includes a fail-closed guardian/registry interface and independent instance/volume cleanup verification.
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
- Make this repository the versioned authority with adopter and development checkpoints.
- Define private holdout isolation and distinguish the known-model low-context challenge from unseen-model evaluation.

## Validation evidence

- At helper revision `c069613ca8b3e02bcf5aba1960160583537f8a3a`, the adopter validator, full CGM adapter validator, and HSW automation check pass.
- `python -m pytest -q`: 150 tests passed on the current working tree. `python -m compileall -q src tests` and `git diff --check` passed.
- Local cleanup now preserves the instance while volume inventory is unavailable and ownership is not yet known, then retries discovery; if that discovery anchor disappears, it keeps the orphan-volume obligation pending. Unresolved-volume-only records are reconciled without deleting uncertain resources. The local worker records heartbeat/attempt/error metadata; an in-host launcher restarts an exited child or one with a stale heartbeat (90 seconds) or stalled tick (one hour), after terminating it. These are local recovery improvements, not independent guardian coverage.
- Fake-only public lifecycle tests now exercise fail-closed guardian readiness, controller and local worker loss, separate guardian/client objects, provider and shared-registry outages, recovery after restore, and idempotent cleanup. They use file-backed test doubles and do not establish deployment, physical failure-domain separation, or real service behavior.
- On Windows, journal byte-lock acquisition now retries `EACCES` contention beyond the CRT's ten-attempt limit and propagates other errors; the regression test exercises more than ten consecutive contention responses.
- The pinned CGM adoption, adapter, and HSW contract checks passed; `continuity validate --root .` returned `VALID`. A54–A56 and M46–M47 remain release gates; the local fake-based tests do not establish deployed guardian recovery or real service behavior.
- GitHub reports `allow_auto_merge=true`. `main` requires the strict `gates` check and one approving review, dismisses stale reviews, enforces the rule for admins, and blocks force-pushes and branch deletion. At head `44d2021a04689baaff07c7a8c66cc403e3702ba3`, the owner-requested PR-level squash auto-merge was enabled after `gates` passed. PR #5 is ready for review but still `BLOCKED` with `reviewDecision=REVIEW_REQUIRED`; GitHub's `autoMergeRequest` is present, and one approving review is outstanding. The latest documented CI run is https://github.com/Pukujan/vastai-gpu-broker/actions/runs/36503451051.
- No paid instance was created and no Vast create/search API call or end-to-end model challenge was run in this turn.

## Blockers and open inputs

- Owner hourly, total, runtime, network and bidding limits for the paid challenge have not been recorded. Their absence must block create.
- Full end-to-end CLI runner, prepared install/inference harness, lease lifecycle real-provider validation, failure injection, and private evaluator remain pending.
- Joint simultaneous-model placement/acceptance and parallel race coordination remain unsupported; do not imply that local test passes establish these modes.
- GitHub auto-merge is enabled and `main` has required `gates` and review protection. PR #5's squash auto-merge request is armed, but required approval is missing and the PR is not merged. Verify the current `autoMergeRequest`, required approval, CI on the current head, and final merged SHA independently.
- The CGM overlay, implementation, docs, and tests are committed on the unmerged branch. The default branch does not contain this revision; this checkpoint does not claim a release.
- Vast documents stop/destruction behavior around zero credits, but permits negative-balance grace and ongoing stopped-disk charges without a fixed deletion deadline. This is billing context only; the broker's cleanup must actively destroy and verify the lease.
- The intended lifecycle is automatic destroy after completion/failure/cancellation/authorized idle deadline. The new local interface requires two declared independent-service guardians, a durable fenced registry readback, and publication of instance/volume IDs before access; physical failure-domain separation is deliberately not inferred from declarations. No external guardian or shared registry backend is provisioned, and A54–A56 outage/recovery tests against deployed services remain open. The local child still cannot guarantee provider deletion during total host/network/API failure; its in-host monitor also ends when the launcher process exits. Jev's local Chrome session and GitHub scheduled workflows do not satisfy guardian readiness. P42/P44/P45 and A50–A56 are not passed. Account-credit exhaustion is not the cleanup plan. Do not treat paid-capable Python APIs as a supported real-money workflow.
- A session should rent one GPU for the requested model/benchmark bundle, switch or co-reside models according to the explicit workload mode, then destroy the ephemeral instance. Keeping an on-demand GPU permanently rented only makes sense after measured utilization and current offers justify its idle-hour cost.

## Next action

Obtain the required approving review and verify that GitHub merges the current PR head using the armed squash auto-merge request. The latest checkpoint commit will rerun `gates`; confirm that result remains successful. Separately, the lifecycle owner must provision the external guardian/shared-registry services and complete A54–A56 recovery tests; verify separate failure domains, registry recovery, real service limits/cost, and owner spend/runtime/network limits before any paid challenge. An auto-merged PR alone does not pass those release gates.
