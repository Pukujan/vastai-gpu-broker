# Plan for complete cost and measured performance

Reviewed 2026-09-28. Architecture owner: Sol. Parent task: VBR-0001, [issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1). Dependencies: exact-model research, market planner VBR-0003, and lease lifecycle VBR-0004. Working branch: `feature/research-first-ephemeral-router`. Its contract additions are integrated into PDD/SDD/TDD as properties P31–P39, acceptance A35–A47 and relations M28–M39, with existing IDs retained. This document does not claim implementation or paid-run evidence.

The owner now sets the network policy at less than $1/TB preferred, $2/TB pushing it, and $3/TB maximum. Older notes disagree: the workspace README permits $1–5/TB and rejects $10/TB, while a model-specific note uses a $1/TB hard cutoff. Neither older rule overrides the current instruction. These numeric tiers are now recorded in PDD/SDD/TDD; the current broker gate still needs tests and release verification.

We should prepare the controller and benchmark package before renting, use disposable instance disk for ordinary requests, and measure inference throughput on the rented GPU host. A separate run from the actual client measures the user's experience over Tailscale. A retained cache and a parallel bid race are later modes with explicit aggregate budgets and cleanup obligations.

## Session strategy and runaway-cost backstops

Do not keep an on-demand GPU rented around the clock just to switch models. Rent one GPU instance for a bounded benchmark/model session, put the exact requested artifacts on that instance's disposable root disk, then switch or load models according to the request's explicit mode. In sequential mode, unload one model before loading the next if the chosen runtime supports it; in simultaneous mode, all requested models stay resident and the whole set must pass fit/acceptance. No separate persistent volume is required for a one-session run. Delete the instance after the result is safely copied out. The root disk is still billable while the instance exists, and a fresh session may repeat model downloads.

Choose on-demand when the owner values prompt allocation/continuous execution enough to accept its quoted premium. Choose interruptible when the task can tolerate pause/retry; Vast documents that these instances can pause when outbid or on-demand is requested and can resume automatically when priority returns, so the lease guardian must still destroy at the authorized deadline. Model switching itself is not a reason to pay for an always-running GPU. Compare a live, exact-offer session total against repeated create/download/warmup costs; do not use a stale price snapshot.

Vast's billing guide documents that zero credits may stop active instances, while disk charges can continue and balances may go negative during a grace buffer; a saved card may be charged, and there is no fixed deletion deadline. This behavior is included in cost-risk reporting only. The broker must actively request destruction after use and the independent guardian must handle agent/controller failure; the design does not wait for balance depletion.

The recovery design needs two cleanup paths in separate control-host failure domains and a durable lease registry both can read. The user's always-on control server is a candidate primary if it is separate from the initiating agent host and its uptime is measured. A hosted recovery worker with a durable registry, such as Cloudflare Workers Cron with D1, is a candidate for the second path; test service startup, registry access, deletion retries, alerting, and monthly cost before relying on it. GitHub Actions can provide a slower reconciliation pass, but not the only guardian: GitHub permits a 5-minute minimum schedule, warns runs can be delayed or dropped, and disables scheduled workflows in public repositories after 60 days without activity. A periodic workflow therefore cannot promise a cleanup deadline. [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows), [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/), [Cloudflare D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/)

Jev UltraFast can attach its Python browser agent to an existing Chrome CDP session, which may let an agent use an already logged-in site interactively. The reviewed fork documents a shared Chrome profile; that session still depends on the machine, browser, and process staying up. It cannot watch or delete a Vast lease after that host fails, so A56 rejects it as a cleanup guardian. This review read the fork's README and design notes; it did not run a browser session. [Jev UltraFast README](https://github.com/Pukujan/jev-ultrafast/blob/d92fcc9/README.md), [Jev design notes](https://github.com/Pukujan/jev-ultrafast/blob/d92fcc9/docs/design.md)

Keep every Vast write key out of the public broker repository. A GitHub Actions secret is not public to readers, but repository writers can change workflows that use it; use a narrowly scoped key and restrict workflow writers. Do not run a secret-bearing guardian on untrusted pull-request code.

Vast Serverless is a possible alternative when the workload is a stable inference endpoint: its managed scaler can be configured to permit zero active or total workers after inactivity. Its pricing guide still bills inactive workers for storage/bandwidth, and says a destroyed endpoint is the state that stops all billing. The Serverless docs also need a configuration check before adoption: the scale-to-zero guide says `min_load=0`, `cold_workers=0`, and a positive `inactivity_timeout` permit zero total workers, while the endpoint-parameters page documents a default `min_workers=5`. Do not assume scale-to-zero from defaults; resolve and verify the effective worker floor. This broker has not integrated or validated Serverless as a replacement for arbitrary one-off, multi-model benchmark sessions. [Managing scale](https://docs.vast.ai/guides/serverless/managing-scale), [endpoint parameters](https://docs.vast.ai/guides/serverless/serverless-parameters), [Serverless pricing](https://docs.vast.ai/guides/serverless/pricing)

## Transfer rates and exact units

Apply the policy to the maximum of the two directional transfer prices. Download and upload must both pass. Do not add the two rates to decide eligibility; multiply each by its actual directional volume when calculating dollars.

| Largest directional rate | Policy result |
|---|---|
| Less than $1/TB | Preferred |
| $1/TB through less than $2/TB | Within cap; disclose departure from the preferred tier |
| $2/TB through $3/TB inclusive | Within cap, expensive tier; show its effect on complete task cost |
| Greater than $3/TB | Reject |
| Unknown unit basis, invalid value, or conflicting directional fields | Unresolved; cannot claim the rate gate passed |

The preference does not authorize a more expensive total task or an invented performance score. Rank eligible candidates by the authorized full-cost objective and deadline, with the network tier visible. A rate cap is different from the task's separate dollar allowance for network usage.

Vast documents `inet_down_cost` and `inet_up_cost` as $/GB, `inet_down` and `inet_up` as MB/s, and `storage_cost` as $/GB/month. Its example response also contains named `internet_*_cost_per_tb` fields. The illustrative up/down per-TB values do not match their same-direction per-GB examples; this example is not conversion evidence. [Search API](https://docs.vast.ai/api-reference/search/search-offers)

Vast's current official CLI source multiplies the host fields `listed_inet_up_cost` and `listed_inet_down_cost` by 1024 when displaying columns labelled $/TB. This establishes that display convention for those fields. It does not establish how many bytes Vast means by a billable GB, or prove the candidate offer's per-TB fields have the same derivation. [Pinned CLI display source](https://github.com/vast-ai/vast-cli/blob/317321568b5d88f765fbdcd5a9ea3578896dc526/vastai/cli/display.py)

Preserve three distinct records: provider raw $/GB, provider native per-TB value when supplied, and any source-backed display conversion. Name the conversion basis in the output, such as `Vast CLI TB display = 1024 provider GB rate units`. Never relabel that quantity as decimal TB or TiB. Exact SI definitions are TB = 10^12 bytes and GB = 10^9 bytes; TiB = 2^40 bytes and GiB = 2^30 bytes. Those definitions do not prove which divisor Vast's billing meter uses.

The broker now uses the explicitly named CLI display-equivalent basis for its owner rate policy: 1024 times each documented raw $/GB rate. This is a policy/display normalization, not a claim that the billing meter defines SI TB, TiB or bytes per GB. Native per-TB values remain separate cross-checks; conflicting same-direction values fail closed. A native-only value needs its own established basis and cannot be relabelled as CLI display-equivalent without evidence. Preserve a mismatch as `NETWORK_UNIT_CONFLICT`; missing basis evidence is `NETWORK_UNIT_BASIS_REQUIRED`. A byte-cost calculation separately requires a sourced `provider_gb_bytes`. Zero rates remain known zero; missing rates remain unknown.

During this review, the coordinator removed the broker's per-TB-to-per-GB divide-by-1000 fallback and the unsupported 730-hour monthly-storage derivation. Current local `market.py` preserves raw per-GB/native per-TB records, classifies the CLI display-equivalent owner tiers, rejects contradictory directional fields, and leaves derived hourly storage unknown. This is observed working-tree code, not a merged-release or test claim. The older workspace CSV fallback still uses 1024 and is not the broker's contract authority. Exact transfer dollars and running/stopped storage derivations still require their own provider byte/month/state evidence.

## A cost ledger for every candidate

Query each offer with exactly the container disk allocation that would be created. The official client supplies `allocated_storage` to the search request. Record allocation, query, rental type, capture time, raw record, and quote digest together. A quote for one allocation cannot authorize another. [Pinned official offer client](https://github.com/vast-ai/vast-cli/blob/317321568b5d88f765fbdcd5a9ea3578896dc526/vastai/api/offers.py)

Report the following fields beside every candidate:

| Cost or resource term | Required basis |
|---|---|
| Provider total per machine-hour | `dph_total` for that exact allocation and rental type |
| Compute per machine-hour | `dph_base`, separate from total |
| Container storage | Exact allocated GB, provider hourly component when documented, raw monthly rate, running and stopped rate distinctions |
| Independent volume | Exact allocated GB, separate hourly quote, lifetime, attachment constraints, deletion deadline |
| Container/runtime download | Pinned image layers, dependencies and harness; missing bytes/cache state stay unknown |
| Model download | Unique exact artifact/base/tokenizer files and revisions; include repeated or resumed downloads separately |
| Dataset download | Exact file manifest, source and compression/transfer state |
| Inference input/output | Serialized request and response byte measurements or an explicitly declared workload estimate; tokens do not equal bytes |
| Other traffic | Logs/results/checkpoints, management, telemetry, transport overhead and retries; measured or unknown |
| Time | Allocation/create, startup, download, install, load, warmup, benchmark, useful runtime, interruption, stop/retention, and cleanup intervals |
| Retry/race exposure | Every potentially owned contract, each cap and its storage/network allowance |
| Local preparation | Measured elapsed time and available resource measurements; monetary cost unknown unless the owner supplies a basis |

For billable running phase p, use the provider's exact-allocation hourly total q_p multiplied by phase seconds/3600. Compute and storage components explain q_p; do not add them again. For stopped/paused retention, use its separately established storage rate. A distinct volume remains additive across its entire retained lifetime. Rates that change with an interruptible bid require a fresh phase quote or a documented decomposition.

For transfer, use C_network = B_down / U_GB * p_down + B_up / U_GB * p_up, where B values are exact integers and U_GB is the provider's documented bytes per billing unit. Each direction retains its own rate and traffic source. If U_GB or a consequential B is unresolved, the task estimate remains unknown. An authorized reserve can bound the plan's intended allowance; it cannot become measured traffic or a provider cutoff.

The task envelope is the sum of billable phase totals, distinct retained storage, directional transfer, allowed failures/retries, and cleanup reserve. Show estimate, authorized maximum and provider-reported charges separately. Startup, downloading and benchmark time consume the same budget as useful inference; prebuilding only moves preparation work and some transfer to other machines.

Vast says compute is charged per second while running, storage continues while the instance exists, stopped storage can be more expensive, and both transfer directions are charged per byte. Its billing FAQ additionally says offline instances incur neither compute nor storage. Preserve this source discrepancy and budget conservatively for storage until verified deletion; do not assume an offline interval is free. [Pricing](https://docs.vast.ai/guides/instances/pricing), [Billing FAQ](https://docs.vast.ai/guides/reference/faq/billing)

For observed charges, use the paginated charges endpoint and reconcile request-owned contract IDs and intervals. It distinguishes GPU, disk, download and upload charges, including volume contracts. Rounded, delayed or incomplete accounting is recorded as such. Balance depletion and a payment/top-up record are not a task cost breakdown. [Charges API](https://docs.vast.ai/api-reference/billing/show-charges)

## Decide retention from measured reuse

Disposable container disk is the initial mode. Its allocation is fixed at creation, survives stop, and is removed with instance destruction. Allocate the exact selected files plus separately evidenced install/cache/temporary needs. Unknown disk overhead must be resolved or enter explicitly bounded validation. Removing files does not reduce an allocated-storage contract. [Storage types](https://docs.vast.ai/guides/instances/storage/types)

For a planned repeat on the same physical machine, compare a bounded local volume or stopped-instance cache with a fresh rental. Local volumes survive instance destruction, have separate billing, are fixed in size, and attach only on the same machine. The attached instance must be deleted before deleting its volume. A retained cache therefore has both a host-availability constraint and a second cleanup obligation. [Volumes](https://docs.vast.ai/guides/instances/storage/volumes)

Measure fresh-download time/bytes and warm-load time/bytes for the exact recipe. Let S be the observed dollar saving per reuse: avoided transfer plus avoided billable GPU-host startup work, minus warm-cache read/copy/validation cost. Let R be the established retention cost per hour. For N explicitly planned reuses over H hours, retaining is cheaper only if N*S exceeds H*R plus cache creation/copy cost and any extra GPU price from remaining on that host. Report the measured terms. If they are missing, the break-even result is unknown. A faster-ready objective can justify retention even when dollar savings alone do not, within its own authorized cap.

Repeated model switching can benefit from a union of pinned artifacts on disk, but it does not establish simultaneous VRAM fit. Disk caches may evict and redownload; record those bytes and times. Changing revision, quantization or runtime invalidates old cache identity and performance evidence. All requested models remain resident together unless the request explicitly selects a switching mode.

An owner-machine cache can move downloads off the rented interval, yet sending files back to a fresh GPU still consumes GPU ingress, local upload time and local resources. Budget both ends when external storage bills egress. Do not claim free local compute, free Tailscale transport, a cached container image, or a portable Vast cache without measurements or source evidence.

Vast documents free internet transit for its instance copy on the same machine/local network; it charges other instance transfers. Apply that exception only to the documented route and established locality. [Data movement](https://docs.vast.ai/guides/instances/storage/data-movement)

The current CLI contains network-volume methods while the user guide says only local volumes are supported. Method existence does not prove cross-machine attachment, availability or price. Keep network volumes outside automatic selection until the current offer/attachment/billing contract is established.

## Prepare locally and measure on the right machine

Build the controller, bounded install recipe, image/harness identity, dataset manifest, acceptance probe, report collector and cleanup supervisor before paid creation. Do a preparation readiness check with a stub endpoint. GPU-specific compilation, model loading, CUDA graph capture and actual inference still belong to the authorized GPU-host budget.

| Location | Recommended role | What it establishes |
|---|---|---|
| User PC | Prepare portable controller/artifacts; operate the broker when its independent supervisor can stay available | Actual PC client experience and local preparation cost/time |
| Linux Gravebuster | Reuse the recorded isolated task/checker harness; possible controller host after availability and dependency discovery | Actual Linux tool/task performance and its own Tailscale route |
| Mac | Run the portable controller/client against the same owned operation; build an appropriate Linux target only with supported tooling | Actual Mac client experience and its own Tailscale route |
| Rented GPU host | Exact-model inference engine and a minimal local benchmark client; export compact results | Host inference and serving performance without the external client WAN path |

The existing `local-hard-bench-harness.md` names a Gravebuster harness and isolated workspaces. That is recorded design context, not proof of current CPU/RAM/disk/network speed, uptime or installed versions. Discover those facts before assigning it work. A Windows or Mac native executable cannot be assumed to run inside a Linux GPU image. Multiple client machines must share a durable coordinator before concurrent broker use.

Keep these measurements in separate result records:

1. Inference-engine throughput on the GPU host, after declared warmup, with exact prompt/output lengths, token accounting, batch/concurrency, model/quant/runtime identity, cache state and device placement. It excludes the external network; it still includes the engine and relevant CPU work. A literal GPU-kernel measurement needs a distinct profiler protocol.
2. HTTP serving performance from a client on the GPU host using localhost, including scheduler, API, serialization and tokenization work. Check client CPU saturation before calling the server saturated.
3. End-to-end request performance from each actual PC/Gravebuster/Mac client over Tailscale: time to first token, inter-token time, total response time, completed/error counts and request concurrency. Do not call this pure GPU tokens/s or subtract one ping value to manufacture GPU time.
4. Agent-task quality and completion time, using isolated workspaces and a separate checker. Checker/tool runtime and model quality remain distinct from inference throughput. CPU-heavy checker jobs on the rented host can contaminate inference measurements; run those on the prepared external harness unless the workload specifically requires colocation.

Use the exact runtime's supported benchmark. If vLLM is the selected compatible runtime, its official tools separate offline throughput, online serving and startup. Its serving tool supports TTFT, token time, inter-token latency, end-to-end latency and JSON results. Pin the runtime version and benchmark protocol; do not install vLLM merely to benchmark a model whose selected runtime is different. [Benchmark modules](https://docs.vllm.ai/en/latest/api/vllm/benchmarks/), [Serving benchmark](https://docs.vllm.ai/en/latest/cli/bench/serve/)

Capture Tailscale path type and latency at the start/end and on observed path changes. Tailscale can start through a relay, become direct, and revert. `tailscale ping`, `status`, and `netcheck` provide route diagnostics; their results do not measure inference. Preserve direct, peer relay and DERP observations beside each client run. [Tailscale performance diagnostics](https://tailscale.com/docs/reference/troubleshooting/poor-performance-tailnet)

Measure exact serialized application bytes separately from transfer-counter deltas. Transport traffic includes headers, encryption, acknowledgments, retransmission, and other processes. Use a documented counter scope, direction, interval and reset/wrap handling; isolate traffic where practical. Provider accounting is a third record. Never call token-derived bytes exact wire bytes or local counter bytes exact provider-billed bytes. If a counter is unavailable, report that measurement unknown. Avoid payload captures in public reports.

## TurboFit as an optional runtime reference

The [TurboFit project](https://github.com/SouthpawIN/turbofit/blob/cd59bbd47165fd7c0358a5c3b63cd51108a6a53e/README.md) describes a Hermes Agent provider, local hardware scan and adaptive inference runtime. Beyond its curated model/tier tables, its repository includes historical benchmark suites and structured model/hardware results, such as the [hardware-tier report](https://github.com/SouthpawIN/turbofit/blob/cd59bbd47165fd7c0358a5c3b63cd51108a6a53e/references/hardware-tier-report.json) and [result artifacts](https://github.com/SouthpawIN/turbofit/tree/cd59bbd47165fd7c0358a5c3b63cd51108a6a53e/references/results). These are useful prior measurements only where the exact host, model artifact/quant, runtime, context, workload, and result status match; they are project-authored and are not independently validated here. Several hardware tiers are explicitly candidates-only, and some saved sweeps contain failures.

TurboFit may be useful after placement as an optional runtime-fit or benchmark reference for an exact compatible artifact, and its historical corpus can help identify prior configurations worth investigating. It is not a broker dependency: the reviewed repository does not provide a Vast offer/billing/bid/lifetime router, and its curated tables or historical results do not replace research for arbitrary requested models. The generic model-size-plus-KV fit rule omits the architecture/runtime/workload-specific and simultaneous-resource evidence required by P05/P28–P30. Do not adopt its fixed KV allowance as a requirement. Any adaptive model substitution or switching must remain inside the user's exact selection/workload authorization. This review did not install or run TurboFit.

## Simultaneous models and parallel bids

All models in the resolved deployment set remain required during fit, hardware preflight and acceptance. Record per-device placement and combined CPU/GPU/disk/cache needs. A one-model success cannot elect a winner for a simultaneous-model request. An authorized validation run must actually hold the entire set resident and exercise its declared concurrent workload.

Start with sequential capped bidding. A future parallel race requires a finite candidate count, maximum concurrent instances/GPUs, per-contract bid caps, aggregate hourly and total caps, aggregate network allowance, exact storage, startup deadline and cleanup reserve. Reserve each candidate's worst authorized startup exposure atomically before dispatch. Account for every contract being able to start and download at once. A bid-floor total cannot be reused after escalation.

Use one durable operation per request ID plus exact deployment/limits digest. Repeated identical calls attach; changed inputs conflict or replan. Race contenders have unique attempt IDs under that operation. A shared coordinator must fence concurrent PC, Linux and Mac writers; independent local journals cannot guarantee cross-machine deduplication.

The first running candidate that passes hardware preflight and the complete simultaneous-model acceptance probe can win. Cancel/destroy every other candidate, including unclear creates, and verify each instance absent. Deleting the GPU instance does not delete a separate volume. Until all loser contracts/volumes are resolved, keep their reserves and report cleanup pending. Winner access may be used only within the still-valid aggregate envelope; never report only one billable contract while loser cleanup is uncertain.

Parallel creation is an explicit bounded exception to the sequential at-most-one rule. P12 now requires one coordinated operation with only its explicitly authorized contenders; retries may never add unintended contenders. Unknown create responses reconcile the same attempt; they do not allocate a replacement.

## Proposed properties and acceptance gates

These requirements are integrated into PDD/SDD/TDD under P31–P39, A35–A47 and M28–M39. P05/P12/P14/P15 also retain their IDs with simultaneous-set, coordinated-race and volume-recovery coverage.

| Property | Observable acceptance requirement |
|---|---|
| Network tiers are durable and direction-complete | Preferred boundary excludes exactly $1; $2–$3/TB remains eligible but visibly expensive; either direction above $3 rejects; cap accepts exactly $3 |
| Units require provenance | SI TB/TiB equivalence works when explicit; native fields and CLI display stay distinct; contradictory/unknown units cannot pass; zero remains distinct from missing |
| Every billable phase appears once | A cheaper compute offer loses when complete planned cost is higher; startup/download/warmup/benchmark, stopped retention and cleanup are included without adding components to `dph_total` again |
| Exact disk binds the quote | Changing allocated container/volume size invalidates the plan; deleting files does not shrink its cost; monthly-to-hourly derivation with no provider divisor remains unknown |
| Cache benefit is measured and bounded | An empty/expired/wrong-revision cache cannot reduce known download cost; retained volume persists after instance deletion and remains an obligation until separately deleted |
| Benchmark protocol scopes every claim | WAN delay changes only client results; local/engine metrics stay separate; cache/prompt/runtime/concurrency changes invalidate evidence; CPU saturation produces a limitation |
| Byte observations name their scope | UTF-8 body bytes, interface bytes and provider accounting never substitute for one another; retries and counter reset/wrap cannot undercount traffic |
| Simultaneous acceptance tests the full set | A partially ready model set cannot win or promote combined fit; switching mode requires explicit scope |
| Race budgets cover all contenders | Simultaneous startup, downloads, bid escalation and loser cleanup cannot exceed reserved aggregate exposure; missing aggregate caps produce zero creates |
| One shared operation survives retries | Duplicate calls from separate machines attach; lost create response reconciles one attempt; changed digest cannot reuse old paid authorization |
| Cleanup includes retained resources | Loser pause/delete acknowledgement, missing pages, timeouts and lingering volume all remain cleanup pending; no reserve is released on uncertain deletion |
| Cost reports preserve estimates and observations | Unknown bytes/rates stay unknown; account top-up never counts as task cost; provider charge pagination and rounding remain visible |

Build order: record the owner policy and unit/source contracts first; implement full phase/traffic/storage planning next; prepare the portable harness and measurement protocol; implement sequential lifetime/cleanup controls; validate those properties against an independent fake provider; run one owner-authorized sequential paid task only after release gates; then consider retained caches and aggregate parallel races. No paid benchmark or rental is authorized by this planning document.

Open inputs are concrete task hourly/total/runtime/network-dollar limits, start/idle timers, exact disk after file/runtime research, benchmark workload, and optional cache/race limits. Unknown provider byte/month/state conventions also remain recorded research gates. This review performed public-source research and local document/code inspection only; it did not access credentials, connect to the named computers, run benchmarks, or rent resources.
