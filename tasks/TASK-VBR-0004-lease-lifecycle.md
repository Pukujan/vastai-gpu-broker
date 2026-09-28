# VBR-0004 — Journaled lease lifecycle

<!-- continuity:task {"acceptance":["Every paid create is preceded by durable intent and two armed cleanup paths in separate control-host failure domains.","Both cleanup paths can read the durable fenced lease registry; missing guardian or registry acknowledgement blocks create.","Lost create responses reconcile owned instances before any retry.","Startup, inference, cancellation, cold/idle/hung and hard deadlines converge on bounded destruction and provider-absence verification.","Failure injection covers each guardian, shared registry, provider/network outage, and safe recovery; unresolved cleanup remains visible and never claims a hard spend cutoff.","Restart recovery preserves unresolved cleanup obligations and never deletes unrelated instances.","Browser-session automation and scheduled GitHub Actions alone fail the guardian-readiness gate."],"depends_on":["VBR-0002","VBR-0003"],"goal":"Implement a journaled, bounded Vast instance lifecycle and independent cleanup supervisors with verified cleanup.","id":"VBR-0004","issue_url":"https://github.com/Pukujan/vastai-gpu-broker/issues/4","next_action":"Design the external guardian and shared-registry interface, add fake-provider failure-injection tests A54-A56, then implement and report signatures and limits to root.","owner":"/root/lifecycle_builder (Luna)","priority":"P0","protocol_version":"0.1.0-draft","schema":"project-continuity.task.v1","status":"active","why":"Unbounded or orphaned instances create real charges, while stopped instances retain storage billing and accepted bids may remain unavailable."} -->

## Progress

Primary writer: `/root/lifecycle_builder`; branch: `feature/research-first-ephemeral-router`; parent issue #1.

## Checkpoint log

Report changed files, public signatures, tests and evidence, blockers, and next action in the owning issue before handoff.
