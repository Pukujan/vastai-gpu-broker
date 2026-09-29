# Official documentation router

Read the linked Vast.ai docs before changing marketplace, billing, API-key, or instance-lifecycle behavior. These are primary sources from Vast.ai. Reviewed 2026-09-28.

## First-party SDK and CLI

- [Vast.ai Python SDK and CLI](https://github.com/vast-ai/vast-cli) — the official package is `vastai`; on Windows it is installed with `pip install vastai`. Its CLI includes `search offers`, `create instance`, `show instance(s)`, and `destroy instance`. The SDK exposes corresponding search, create, show, and destroy methods. Reviewed 2026-09-29.
- [SDK lifecycle reference](https://github.com/vast-ai/vast-cli/blob/master/vastai_sdk/SKILL.md) — documents `VastAI.search_offers`, `create_instance`, `show_instance`/`show_instances`, and `destroy_instance`, plus `SyncClient` equivalents.

The broker now presents those lifecycle verbs as `vast-broker search`, `create`, `status`, and `destroy`. These are broker operations: `create` runs the pinned, router-authorized Open-Jev workflow; `status` and `destroy` require a request ID in the private lease journal. The provider adapter uses Vast's documented API routes so the broker can retain explicit pagination checks, sanitize responses, and verify full account inventory and separate volume absence. The first-party CLI/SDK are useful for standalone Vast administration, but a direct create/destroy call does not perform this broker's spend authorization, durable ownership, or post-destroy verification.

## Marketplace and offers

- [Search offers API](https://docs.vast.ai/api-reference/search/search-offers) — `POST /api/v0/bundles`; type values are `ondemand`, `bid`, and `reserved`; fields include total hourly price, storage, bandwidth rates, GPU/RAM/disk, verification, and current rentable state. The `duration` filter is a minimum remaining offer duration, not a lease timeout.
- [Finding & renting instances](https://docs.vast.ai/guides/instances/choosing/find-and-rent) — offer card interpretation and availability.
- [Instance types](https://docs.vast.ai/guides/instances/choosing/instance-types) — the UI creates on-demand or interruptible instances; reserved is a post-create conversion of an on-demand instance with prepaid credits and a commitment term. Reserved has high priority like on-demand but is not a direct offer-acceptance mode.
- [Reserved instances](https://docs.vast.ai/guides/instances/choosing/reserved-instances) — conversion/prepayment steps, commitment periods, pricing preview, and early-cancellation refund/clawback behavior.

The saved JSON file in this repository is only a timestamped cache. Search again before a create request because listings and prices can change. Do not copy the docs' example `verified=true` filter into our search; this user wants unverified and deverified offers included.

Search API `type=reserved` remains useful as a separate pricing observation. It does not authorize a direct create or establish the later conversion's exact prepayment/term. The broker's ordinary offer ranking excludes reserved rows from direct-create eligibility; reserved conversion is unsupported until an explicit owner authorization, exact commitment quote/term, and separate conversion operation are available.

## Cost and instance lifecycle

- [Pricing](https://docs.vast.ai/guides/instances/pricing) — compute billed per second while running; storage billed continuously while instance exists, including stopped; data transfer billed by bytes in both directions.
- [Billing](https://docs.vast.ai/guides/reference/billing) — when credits reach $0, instances stop automatically and storage/volumes/data are scheduled for deletion; disk charges may continue while stopped and the account may go negative during a grace buffer based on average daily spend. A saved card is periodically charged for negative balances; without a saved card, Vast says it destroys instances/data to prevent indefinite unpaid usage. Balances update about every few seconds. This is a provider-side compute brake, not a hard final-bill cap or a documented deletion deadline.
- [Billing FAQ](https://docs.vast.ai/guides/reference/faq/billing) — describes storage billing while an instance is online, and says storage is not charged while it is offline. This differs from the Pricing guide's broader statement that storage is billed while the instance exists. Until provider behavior for the selected host/state is confirmed, retain the conservative cost reserve and verify deletion.
- [Storage types](https://docs.vast.ai/guides/instances/storage/types) — container disk is fixed at creation, lost on instance destroy, and the guide states a 10 GB minimum/default. The Search Offers API example says `allocated_storage` defaults to 8 GB; therefore query and bind the actual requested allocation rather than assuming one default.
- [Volumes](https://docs.vast.ai/guides/instances/storage/volumes) — separately priced storage survives instance deletion but is tied to the physical host; the guide says the offer must be on the same machine. Delete the attached instance before deleting the volume. The guide exposes pricing per offer rather than a universal price.
- [Search Offers API](https://docs.vast.ai/api-reference/search/search-offers) — `inet_up_cost`/`inet_down_cost` are described as $/GB and `inet_up`/`inet_down` as network speeds; the example also contains `internet_*_cost_per_tb`, but its per-TB sample values do not match the same-direction per-GB examples. Treat mismatches as a unit conflict.
- [Pinned Vast CLI display source](https://github.com/vast-ai/vast-cli/blob/317321568b5d88f765fbdcd5a9ea3578896dc526/vastai/cli/display.py#L1606-L1608) — `show machines` labels `listed_inet_*_cost` as $/TB and multiplies by 1024. This proves the CLI display convention for those fields, not the actual byte divisor Vast uses for billing or that all API fields have the same mapping.
- [Vast charge history API](https://docs.vast.ai/api-reference/billing/show-charges) — use actual provider charge records to reconcile GPU, disk, and transfer costs; quote math remains an estimate until reconciled.
- [Managing instances](https://docs.vast.ai/guides/instances/manage-instances) — stop preserves data and storage charges continue; destroy permanently removes data; restarting may not reclaim the same GPU.
- [Create instance API](https://docs.vast.ai/api-reference/instances/create-instance) — `PUT /api/v0/asks/{offer_id}`; specify image/template, disk and target state. Set `cancel_unavail` explicitly: docs say defaults differ by rental type (false for interruptible, true for on-demand with target state running).
- [Show instance API](https://docs.vast.ai/api-reference/instances/show-instance) — re-read state by owned instance ID.
- [Show instances API](https://docs.vast.ai/api-reference/instances/show-instances) — `GET /api/v1/instances` returns `instances`, `total_instances`, and a `next_token`; it uses keyset pagination with a maximum 25 rows per page. Continue until `next_token` is null before using a complete account listing to verify absence. A failed, malformed, count-inconsistent, or page-capped listing is not evidence of deletion.
- [Manage instance API](https://docs.vast.ai/api-reference/instances/manage-instance) — start/stop and label operations.
- [Destroy instance API](https://docs.vast.ai/api-reference/instances/destroy-instance) — `DELETE /api/v0/instances/{id}`; destructive removal.
- [Notifications](https://docs.vast.ai/guides/reference/notifications) and [notification webhooks](https://docs.vast.ai/guides/reference/notification-webhooks) — renter lifecycle events can be delivered to an HTTPS endpoint we operate; they are signals for our recovery system, not provider-enforced deletion or a substitute for an available, authorized cleanup controller.
- [Vast CLI hello world](https://docs.vast.ai/cli/hello-world) — operational create/connect/monitor/cleanup walkthrough; it explicitly says destroy stops billing and stop leaves disk charges.

The current create API documents no maximum-runtime or auto-destroy field. Its request has settings such as `target_state`, `price`, `onstart`, and `disk`, but no lease TTL; `DELETE /api/v0/instances/{id}` is a separate API operation that requires an Authorization API key. The management guide says expired instances *may* be deleted 48 hours after expiration, which is not a configurable, immediate, or deterministic auto-destroy guarantee. A Vast notification webhook can deliver lifecycle events to an HTTPS endpoint we operate, but that endpoint still needs to be alive and authorized to request deletion. A child cleanup process on the initiating host covers caller-process death only; a second guardian outside the GPU host is needed to cover GPU/container failure, and another control-host failure domain is needed to cover the initiating host. Even then, total controller/network/provider outage has no guaranteed deletion time. Do not expose the account key to the rented guest, call a timeout or destroy acknowledgement "verified cleanup," or represent the user's max-spend input as an absolute provider-enforced bill ceiling.

## API key permissions

- [Manage API keys](https://docs.vast.ai/guides/reference/api-keys) — default keys have full account access; use scoped keys.
- [Permissions reference](https://docs.vast.ai/api-reference/permissions) — `misc` includes searching offers; `instance_read` reads/monitors; `instance_write` creates, manages, and destroys instances. No billing-write permission is needed for this broker.

Use one read-only `misc` key for listing refresh and a separate key scoped to only `misc`, `instance_read`, and `instance_write` for a future lease controller. Never store a provider key in the repository, even encrypted. A GitHub Actions secret is not readable by public repository visitors, but workflows can use it and repository writers can change workflows that access it; use a narrowly scoped key and restrict writers. Do not run secret-bearing workflows on untrusted pull-request code.

## External guardian service limits

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) — schedules have a 5-minute minimum, may be delayed or dropped under load, run from the default branch, and public-repository schedules can be disabled after 60 days without repository activity. GitHub-hosted jobs also have a six-hour maximum. Treat Actions as an extra reconciliation path, not the sole lease guardian.
- [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) — each job runs on a fresh hosted machine, so live state must come from an external durable registry.
- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/) — supports scheduled Worker runs, including minute-level schedules. Configuration changes can take time to propagate; this is a candidate service, not a Vast cleanup guarantee.
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) and [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) — document the execution quotas and database allowances to include in the service design and cost test. Quotas and availability do not guarantee that a cleanup request reaches Vast.

The broker requires two cleanup paths in separate control-host failure domains before paid create. Both need access to the durable fenced lease record and authorized provider cleanup credentials. Neither scheduled automation nor a logged-in browser session creates a provider-enforced maximum lifetime.

## Model requirements and official evidence

- [Hugging Face model cards](https://huggingface.co/docs/hub/model-cards) — a model repository's `README.md` is its model card. The documentation recommends reporting model purpose, limitations, training information, and evaluation results.
- [Annotated model card template](https://huggingface.co/docs/hub/model-card-annotated) — technical specifications, including `hardware_requirements`, are optional model-card sections.

Therefore a missing numeric memory or GPU requirement is not a published minimum. Inspect the exact model card, immutable revision, configuration and selected artifact manifest first. If the publisher does not give the hardware requirement, search applicable official runtime documentation and exact-artifact deployment reports; record a tested configuration as tested evidence, keep a minimum unknown unless a source states it, and do not convert a parameter-count or file-size estimate into a fit claim. Estimates may inform an explicitly authorized validation plan, but cannot by themselves make an offer eligible.

## Optional OSS evaluation

- [SouthpawIN/TurboFit](https://github.com/SouthpawIN/turbofit) — its README describes a Hermes-specific local adaptive inference provider with hardware scanning, model selection/acquisition, and benchmark tooling; its repository lists MIT licensing. The repo also has historical structured model/hardware benchmark records under `references/`, including candidates-only tiers and some measured profiles. These can be historical comparison evidence only, not universal fit guarantees, current Vast pricing, or exact official Hugging Face requirements. It has no documented Vast offer, bidding, network/storage cost, or lease cleanup role. Do not adopt it as the broker's router or fit authority. Hardware preflight and exact-profile benchmark concepts may be used as design references, with this broker's source/evidence contract still controlling.

## Agent and serverless options

- [Using Vast.ai with Agents](https://docs.vast.ai/guides/get-started/agents) — Vast's agent-facing skill/CLI and integrations.
- [Serverless overview](https://docs.vast.ai/guides/serverless) — endpoint/worker model and auto-scaling.
- [Serverless setup](https://docs.vast.ai/guides/serverless/setting-up-endpoints) — endpoint options and inactivity timeout.
- [Workergroup parameters](https://docs.vast.ai/guides/serverless/workergroup-parameters) — `gpu_ram` defaults to 24 when omitted, so it must never be left implicit when exact model documentation is the selection source.
- [Serverless pricing](https://docs.vast.ai/guides/serverless/pricing) — Ready and Loading workers incur compute/storage/bandwidth; Creating and Inactive workers have no compute charge but still storage/bandwidth; only a Destroyed endpoint stops all worker billing.
- [Managing Serverless scale](https://docs.vast.ai/guides/serverless/managing-scale) and [endpoint parameters](https://docs.vast.ai/guides/serverless/serverless-parameters) — `inactivity_timeout` can permit scale-to-zero with `min_load=0` and `cold_workers=0`, but the endpoint parameters page gives `min_workers=5` as its default. Verify the complete effective worker-floor configuration before relying on zero active/total workers; scale-to-zero does not stop inactive-worker storage charges or destroy the endpoint.

Serverless is useful to compare for a stable, recurring inference endpoint and may reduce idle GPU compute using Vast's managed scaler. It is not the default for this user's ad hoc ephemeral multi-model benchmark requests: inactive workers still have storage/network charges, its zero-worker settings require verification, and endpoint destruction is required for full cleanup. It has not been integrated or validated as a fit for arbitrary model switching and local benchmark execution. Start with an agent-invoked CLI and a read-only listing cache; add a web service only if concurrent agents later need shared lease coordination.
