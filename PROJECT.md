# Vast GPU Broker — Project Contract

<!-- continuity:project {"id":"vastai-gpu-broker","protocol_version":"0.1.0-draft","schema":"project-continuity.project.v1","title":"Vast GPU Broker"} -->

## Goal

Provide a reusable, research-first module and command-line tool that helps agents host requested open models ephemerally on Vast.ai. For each request it resolves the exact model artifact and workload, gathers model-specific official and external evidence, compares current compatible GPU offers and costs, enforces explicit authorization and limits, deploys only the approved choice, then destroys the lease and verifies cleanup.

## Authority

GitHub issue [VBR-0001](https://github.com/Pukujan/vastai-gpu-broker/issues/1) owns overall task scope and progression. Child issues own bounded research, market, and lifecycle work. Merged default-branch history owns accepted contracts and code. This file, `checkpoints/CURRENT.md`, and `tasks/` are machine-validated projections. User authorization/limits and external publisher/provider evidence retain their stated authority and scope.

## Principles

- Resolve ambiguous model requests to sourced candidate artifacts; preserve exact URLs/revisions/quantizations and ask only when user choice is genuinely still open.
- Exact candidate requests and scoped auto-selection authorization do not trigger redundant model confirmation.
- Separate publisher requirements, measured artifact facts, formula-derived estimates, external tested configurations, local measurements, and unknowns. Never guess a minimum.
- Rank fresh eligible offers by explicit constraints and transparent compute/storage/network prices. Include all rental and verification states; label incomplete market results.
- Do not create without valid authorization and concrete spend/runtime/network/bid limits. Missing limits are input blockers, not implied budgets.
- Persist and supervise every paid lease; stop retains storage charges, while destroy deletes data. Verify provider absence.
- Keep holdout evaluator inputs outside builder and challenge-agent context.

## Current implementation phase

Research-first model routing, current marketplace planning, and bounded lease lifecycle are in implementation under child issues #2–#4. Read `AGENTS.md`, `SPEC.md`, `docs/SDD.md`, and `checkpoints/CURRENT.md` before acting.
