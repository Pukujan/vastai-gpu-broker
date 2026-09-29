# VBR-0003 — Vast offer pricing planner

<!-- continuity:task {"acceptance":["Live offer retrieval accounts for pagination, filter completeness, rental type and verification status.","Quotes normalize machine-hour, storage, disk and network units without double counting.","Candidate-specific unique sample counts and min/median/mean prices are reported by rental type with an as-of timestamp.","Eligibility precedes price ranking; capped results cannot claim a global market minimum."],"depends_on":["VBR-0002"],"goal":"Implement a current, transparent Vast offer comparison and price planner scoped to researched model candidates.","id":"VBR-0003","issue_url":"https://github.com/Pukujan/vastai-gpu-broker/issues/3","next_action":"Implement isolated market/provider module and fake-provider tests, then report public signatures to root.","owner":"/root/market_builder (Luna)","priority":"P0","protocol_version":"0.1.0-draft","schema":"project-continuity.task.v1","status":"active","why":"Model-specific GPU choice depends on live availability and compute, storage, and network prices; a stale or compute-only list can misroute paid work."} -->

## Progress

Primary writer: `/root/market_builder`; branch: `feature/research-first-ephemeral-router`; parent issue #1.

## Checkpoint log

Report changed files, public signatures, tests and evidence, blockers, and next action in the owning issue before handoff.
