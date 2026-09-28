# Current checkpoint

<!-- continuity:current {"active_task":"VBR-0001","active_task_file":"tasks/TASK-VBR-0001-research-first-ephemeral-runner.md","protocol_version":"0.1.0-draft","schema":"project-continuity.current.v1"} -->

Updated: 2026-09-28. Contract target: SPEC/PDD/SDD/TDD version 1; implementation schema is pending.

Source authority: `https://github.com/Pukujan/vastai-gpu-broker` (visibility verified public). Working branch: `feature/research-first-ephemeral-router`. Commit `73a3ac8` is pushed and open as draft PR #5. Issue #4 records the two-guardian owner requirement. The branch is not merged; this checkpoint does not claim a release.

## Content system pin

- Helper source: [`Pukujan/content-generation-modules`](https://github.com/Pukujan/content-generation-modules) at version `0.5.7`, exact commit `c069613ca8b3e02bcf5aba1960160583537f8a3a`.
- The full eight-module pin and adapter are recorded in `.content-system/`; helper implementation/history is checked out at that revision for validation and is not vendored.
- `AGENTS.md` carries the helper's exact always-on writing block. `scripts/validate_cgm_adoption.py` checks the pin, boot block, image source record, and asset digest; CI also runs the pinned helper's adapter and HSW checks.
- The adoption overlay and tests are committed on the draft PR branch, not merged. This pin does not imply that every planned broker feature is implemented or that a paid challenge passed.

Canonical task: [VBR-0001 / issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1). This file is a projection; the continuity overlay may preserve its content in the target's canonical checkpoint layout.

## Actual capability

- The working tree now contains a Python broker package with exact Hugging Face candidate resolution, evidence validation, live Vast offer retrieval/normalization, cost comparison, bounded offer caching, a review/authorization gate, provider adapters, a lease journal/supervisor, and a timestamped snapshot workflow that proposes a PR rather than pushing snapshots to `main`.
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
- `python -m pytest -q`: 96 tests passed. `python -m compileall -q src tests`: passed. GitHub `gates` passed on PR #5 at commit `73a3ac8`.
- The pinned CGM adoption, adapter, and HSW contract checks passed; `continuity validate --root .` returned `VALID`; `git diff --cached --check` passed before commit. A54–A56 and M46–M47 are specified in TDD but remain unimplemented and untested in executable code.
- GitHub reports `allow_auto_merge=true`. The `main` branch currently has no protection rule, so required `gates` enforcement has not been configured. Draft PR #5 is open, has a passing `gates` run, and is not merged or set to auto-merge.
- No paid instance was created and no Vast create/search API call or end-to-end model challenge was run in this turn.

## Blockers and open inputs

- Owner hourly, total, runtime, network and bidding limits for the paid challenge have not been recorded. Their absence must block create.
- Full end-to-end CLI runner, prepared install/inference harness, lease lifecycle real-provider validation, failure injection, and private evaluator remain pending.
- Joint simultaneous-model placement/acceptance and parallel race coordination remain unsupported; do not imply that local test passes establish these modes.
- GitHub `allow_auto_merge` is enabled, but `main` protection/required `gates` check enforcement is absent and must be addressed before auto-merging a release PR.
- The CGM overlay, implementation, docs, and tests are committed on the unmerged branch. The default branch does not contain this revision; this checkpoint does not claim a release.
- Vast documents stop/destruction behavior around zero credits, but permits negative-balance grace and ongoing stopped-disk charges without a fixed deletion deadline. This is billing context only; the broker's cleanup must actively destroy and verify the lease.
- The intended lifecycle is automatic destroy after completion/failure/cancellation/authorized idle deadline. New owner acceptance P45/A54–A56 requires two cleanup paths in separate control-host failure domains, tied to a durable lease registry. The current lease child has no continuing heartbeat, the parent does not watch its liveness after startup, and worker exceptions are suppressed. No external guardian or shared registry is implemented. Jev's local Chrome session and GitHub scheduled workflows do not satisfy guardian readiness. P42/P44/P45 and A50–A56 are documented but not implemented or passed. Account-credit exhaustion is not the cleanup plan. Do not treat paid-capable Python APIs as a supported real-money workflow.
- A session should rent one GPU for the requested model/benchmark bundle, switch or co-reside models according to the explicit workload mode, then destroy the ephemeral instance. Keeping an on-demand GPU permanently rented only makes sense after measured utilization and current offers justify its idle-hour cost.

## Next action

Keep PR #5 in draft while the lifecycle owner defines the external guardian/shared-registry interface and implements executable tests for A54–A56. Then verify separate failure domains, registry recovery, real service limits/cost, branch protection, and required `gates` enforcement before marking the PR ready or considering merge. No paid challenge may run before those gates pass.
