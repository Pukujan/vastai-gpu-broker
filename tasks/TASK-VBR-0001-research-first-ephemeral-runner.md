# VBR-0001 — Research-first ephemeral Vast GPU runner

<!-- continuity:task {"acceptance":["A clean adopter reaches model research, candidate selection and explicit cost gates through one module/CLI.","Current Vast offers are compared with sourced model/workload requirements and transparent price/network evidence.","Only bounded authorized leases may run; completion/failure leads to verified destruction.","Metamorphic and private holdout checks are isolated; auto-merge delivery is verified on GitHub."],"depends_on":[],"goal":"Build a reusable research-first module and CLI that routes model-hosting tasks to the least-cost supported Vast GPU and safely destroys it after use.","id":"VBR-0001","issue_url":"https://github.com/Pukujan/vastai-gpu-broker/issues/1","next_action":"Integrate child modules, establish gates and run offline acceptance before the minimal-context paid challenge.","owner":"/root coordinator; architecture owner Sol","priority":"P0","protocol_version":"0.1.0-draft","schema":"project-continuity.task.v1","status":"active","why":"Low-context agents must act on exact model evidence, live price constraints and durable cleanup rather than guesses or stale offer lists."} -->

## Scope

The GitHub issue owns overall acceptance. Child issues #2–#4 cover research, market planning, and lease lifecycle implementation. This root task covers coordination, shared CLI/router, continuity adoption, CI/release gates, review, and independent evaluation.

## Progress

- Planning contracts created: `SPEC.md`, `docs/PDD.md`, `docs/SDD.md`, `docs/TDD.md`.
- Luna builders dispatched on child issues #2–#4.
- Owner hourly/total/runtime/network/bid limits are not recorded; paid create must remain blocked until available.

## Next checkpoint

Integrate and validate builder modules; update this record with actual test and merge evidence before the fresh-agent challenge.


## Checkpoint log

- 2026-09-28: planning contracts and child issues created; implementation and gates are in progress. No paid instance created.
