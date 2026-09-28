# Official documentation router

Read the linked Vast.ai docs before changing marketplace, billing, API-key, or instance-lifecycle behavior. These are primary sources from Vast.ai. Reviewed 2026-09-28.

## Marketplace and offers

- [Search offers API](https://docs.vast.ai/api-reference/search/search-offers) — `POST /api/v0/bundles`; type values are `ondemand`, `bid`, and `reserved`; fields include total hourly price, storage, bandwidth rates, GPU/RAM/disk, verification, and current rentable state. The `duration` filter is a minimum remaining offer duration, not a lease timeout.
- [Finding & renting instances](https://docs.vast.ai/guides/instances/choosing/find-and-rent) — offer card interpretation and availability.
- [Instance types](https://docs.vast.ai/guides/instances/choosing/instance-types) — on-demand, reserved, interruptible behavior and priority.

The saved JSON file in this repository is only a timestamped cache. Search again before a create request because listings and prices can change. Do not copy the docs' example `verified=true` filter into our search; this user wants unverified and deverified offers included.

## Cost and instance lifecycle

- [Pricing](https://docs.vast.ai/guides/instances/pricing) — compute billed per second while running; storage billed continuously while instance exists, including stopped; data transfer billed by bytes in both directions.
- [Managing instances](https://docs.vast.ai/guides/instances/manage-instances) — stop preserves data and storage charges continue; destroy permanently removes data; restarting may not reclaim the same GPU.
- [Create instance API](https://docs.vast.ai/api-reference/instances/create-instance) — `PUT /api/v0/asks/{offer_id}`; specify image/template, disk and target state. Set `cancel_unavail` explicitly: docs say defaults differ by rental type (false for interruptible, true for on-demand with target state running).
- [Show instance API](https://docs.vast.ai/api-reference/instances/show-instance) — re-read state by owned instance ID.
- [Manage instance API](https://docs.vast.ai/api-reference/instances/manage-instance) — start/stop and label operations.
- [Destroy instance API](https://docs.vast.ai/api-reference/instances/destroy-instance) — `DELETE /api/v0/instances/{id}`; destructive removal.
- [Vast CLI hello world](https://docs.vast.ai/cli/hello-world) — operational create/connect/monitor/cleanup walkthrough; it explicitly says destroy stops billing and stop leaves disk charges.

No duration parameter in the create API docs is documented as a maximum lifetime or auto-destroy timer. Search `duration` only says the offer remains available at least that long. A future lease controller needs its own bounded deadline and recovery supervisor. Client-side cleanup cannot promise success after host, process, or network loss, so it must persist lease IDs and reconcile them after recovery; do not claim a hard no-charge guarantee.

## API key permissions

- [Manage API keys](https://docs.vast.ai/guides/reference/api-keys) — default keys have full account access; use scoped keys.
- [Permissions reference](https://docs.vast.ai/api-reference/permissions) — `misc` includes searching offers; `instance_read` reads/monitors; `instance_write` creates, manages, and destroys instances. No billing-write permission is needed for this broker.

Use one read-only `misc` key for listing refresh and a separate key scoped to only `misc`, `instance_read`, and `instance_write` for a future lease controller. Do not store a full-access account key in GitHub or the repository. A secret used by GitHub Actions can be accessed by workflows and those with repository write permission, so keep that repo private and restrict writers.

## Agent and serverless options

- [Using Vast.ai with Agents](https://docs.vast.ai/guides/get-started/agents) — Vast's agent-facing skill/CLI and integrations.
- [Serverless overview](https://docs.vast.ai/guides/serverless) — endpoint/worker model and auto-scaling.
- [Serverless setup](https://docs.vast.ai/guides/serverless/setting-up-endpoints) — endpoint options and inactivity timeout.
- [Workergroup parameters](https://docs.vast.ai/guides/serverless/workergroup-parameters) — `gpu_ram` defaults to 24 when omitted, so it must never be left implicit when exact model documentation is the selection source.
- [Serverless pricing](https://docs.vast.ai/guides/serverless/pricing) — Ready and Loading workers incur compute/storage/bandwidth; Creating and Inactive workers have no compute charge but still storage/bandwidth; only a Destroyed endpoint stops all worker billing.

Serverless is useful to compare for a stable, recurring inference endpoint. It is not the default for this user's ad hoc ephemeral multi-model requests: inactive workers still have storage/network charges and endpoint destruction is required for full cleanup. Start with an agent-invoked CLI and a read-only listing cache; add a web service only if concurrent agents later need shared lease coordination.
