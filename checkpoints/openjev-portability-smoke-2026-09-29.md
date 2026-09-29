# The Open-Jev smoke paused after one offer search

The isolated broker CLI returned 100 on-demand offers on 29 September. The provider marked the snapshot incomplete because the single page reached its 100-offer limit. We did not create a GPU instance.

## Owner's request

The owner authorized one Open-Jev 9B smoke run. The model must answer live for at least three minutes, then the broker must destroy the instance and attached volumes and verify their absence. The run is capped at $0.20 total and $0.20 per hour all-in, with network transfer around $1–2 per TB. A full benchmark is outside this task.

The local Vast credential is named `vastai_2`. It was read into the search process only. The credential value and raw offer records do not belong in GitHub. The owner has already authorized the bounded run; do not ask for that authorization again.

## Search result

The read-only search used on-demand offers, one GPU, at least 24 GB of GPU memory, a $0.20 hourly machine ceiling, and a 60 GB storage estimate. It returned 100 rows: 58 verified, 22 unverified, and 20 deverified. The result is truncated, so it is not a complete market comparison or a create quote. The query did not filter network prices; the broker must include transfer in the all-in quote before any paid action. A new computer must refresh the market.

The normalized result stayed on the original computer. Its SHA-256 is `2d12246ef0a671d51b10054b8f0ba21338bbab67ec8b20bc3c33aa097f47add0`. No create command ran, no instance was created or found in this task, and no GPU charge resulted. We did not run a provider-wide absence query because there was no request ID.

## Adapter friction found

The `search --filters` help called its input a JSON object, while the command reads a JSON file path. Passing inline JSON produced a local path error before any marketplace request. Adding `rentable` to the file produced a clear rejection because the command owns marketplace-status filters. The CLI help now names the file-path requirement and explains which fields its flags control.

## Resume on another computer

1. Pull the broker repository and read its current checkpoint and issue #1 before acting. Rebuild the isolated package from the checked-in source; the temporary wheel was not committed.
2. Load the authorized local `vastai_2` credential into the process environment expected by the broker. Keep it in the machine's local secret store and out of GitHub.
3. Refresh the on-demand market with the saved constraints. Treat the old 100-row snapshot as historical evidence, not a launch quote.
4. Continue through the broker's guarded Open-Jev 9B lifecycle only if its current readiness gates pass and the exact quote fits the owner's caps. Do not call Vast directly or bypass a guard.
5. If create succeeds, keep inference live for at least three minutes, destroy the instance and attached volumes, and verify provider-side absence. Record the receipt and spend in issue #1.

The last verified broker checkpoint still required two deployed recovery guardians in separate failure domains and their shared registry before paid creation. This smoke did not deploy or recheck those services. If that gate is still closed on resume, stop before spend and record its exact output.

## Resume on the owner's second Mac (2026-09-29)

The owner switched devices, raised the run cap to $2 total (still $0.20 per hour all-in, network around $1–2 per TB), forbade Cloudflare R2 for this day, and pointed to this machine's local Vast key. Resume steps 1–3 were completed: a fresh clone at `5c2090e`, the package installed from source with the recovery extra, and the key loaded into the process environment only. The key line on this device is named `vastai_api`; no `vastai_2` entry exists here, so the earlier checkpoint's key name did not carry over.

The first read-only search failed: `search --filters` sends the JSON file to Vast unchanged, so constraint-style names are rejected with a masked HTTP 400. Only the native operator form works, for example `{"gpu_name":{"eq":"RTX 5060 Ti"},"num_gpus":{"gte":1},"gpu_ram":{"gte":15000}}`. With that shape the search returned 100 on-demand RTX 5060 Ti offers (one page, `complete=false`; 70 verified, 22 unverified, 8 deverified; cheapest verified offer $0.074/hour with preferred-tier network rates). The snapshot stayed local.

The guarded `create` attempt then ran with the scoped credentials, control-host id, and a private journal directory all set, and with no guardian gate configured. It refused before any provider call, exit code 2, with exactly: `two remote recovery paths and their shared registry are not configured`. No proposal, no journal record, no instance, no charge. Per resume step 4 and the stop-before-spend rule above, the run ends here on this device. Two additional readiness gaps were recorded in `CURRENT.md`: the pinned recipe issues a single inference probe and cannot yet satisfy "live for at least three minutes", and the GPU preflight runs after billing has started.

## Record details

- Owning task: VBR-0001, issue #1; parent: none.
- Dependencies: broker adapter, a local Vast credential with the required permissions, and the broker's recovery-readiness gate.
- Source revision used to build the isolated wheel: `8f9416eb169e508c4101ee2c5be74f7c75d1beea`.
- Isolated wheel SHA-256: `8e86c0269a88b403e8a80447209fc5ae8807487e0bf09a839d8cea630cc4eb29`.
- Task runner: a context-isolated subagent using the temporary workspace; it did not inspect the ACS checkout.
- Search snapshot: captured `2026-09-29T18:05:50.913203+00:00`; 100 rows, one page, `complete=false`.
- Merged to `main` via [PR #19](https://github.com/Pukujan/vastai-gpu-broker/pull/19) at commit `6ccd2f8cf4660fe3e819ee88a238314d94875de0`; the required `gates` check passed. The focused CLI help test passed (1 passed, 7 deselected).
- The full test suite was not run locally.
