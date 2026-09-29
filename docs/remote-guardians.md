# Remote recovery guardians

The lease gate requires two independently hosted cleanup paths to read the same fenced R2 lease registry and to acknowledge each create intent before Vast creation. Each path must be able to enumerate the account's instances and volumes, reconcile an uncertain create, destroy only the request's resources, and verify fresh provider-side absence. A local child process, browser, or GitHub Actions schedule alone does not qualify.

## Persistent HTTP service

`Dockerfile.guardian` runs `vast-broker-guardian`. Deploy it in a failure domain separate from the initiating host and from the other recovery path. Terminate TLS at the hosting platform or a reverse proxy; the broker refuses non-HTTPS remote guardian URLs. Keep the service private where possible and use a random bearer token of at least 32 characters. Provide an HTTPS alert destination that accepts idempotency keys.

Required service environment:

| Variable | Purpose |
| --- | --- |
| `VAST_API_KEY` | Scoped Vast key: `misc`, `instance_read`, `instance_write`; no billing-write permission. |
| `VAST_BROKER_R2_BUCKET` | Private R2 bucket for durable lease records. |
| `VAST_BROKER_R2_ENDPOINT` | R2 S3-compatible endpoint URL. |
| `VAST_BROKER_R2_ACCESS_KEY_ID` | R2 object access key. |
| `VAST_BROKER_R2_SECRET_ACCESS_KEY` | R2 object secret. |
| `VAST_BROKER_R2_ACCOUNT_ID` | Cloudflare account ID used to derive the endpoint when needed. |
| `VAST_BROKER_GUARDIAN_ID` | Stable unique guardian identity. |
| `VAST_BROKER_CONTROL_HOST_ID` | Stable identity for this control service. |
| `VAST_BROKER_FAILURE_DOMAIN_ID` | Hosting failure-domain identity, distinct from the initiating host and second guardian. |
| `VAST_BROKER_GUARDIAN_HTTP_TOKEN` | Service bearer token shared with the initiating broker for authenticated acknowledge/reconcile requests. |
| `VAST_BROKER_ALERT_WEBHOOK_URL` | HTTPS alert sink. |
| `VAST_BROKER_ALERT_WEBHOOK_TOKEN` | Optional alert-sink bearer token. |

Optional tuning: `VAST_BROKER_GUARDIAN_BIND` (defaults to `0.0.0.0`), `PORT` (defaults to `8080`), and `VAST_BROKER_GUARDIAN_POLL_SECONDS` (defaults to `30`, allowed range 1–3600). The worker checks provider inventories and registry access before it starts serving. `/health` is a process check only; it does not prove recovery readiness. The scheduler continuously sweeps the registry and the HTTP API handles readiness acknowledgement and explicit reconciliation.

## Second path on GitHub Actions

`.github/workflows/vast-guardian.yml` is a slower second recovery path alongside the persistent service. It uses the trusted `main` workflow code, runs a five-minute schedule and can be dispatched for an intent acknowledgement or immediate reconciliation. It is disabled unless the repository variable `VAST_BROKER_GUARDIAN_ENABLED` is exactly `true`, and its secret-bearing job runs only in a private repository. This broker's source repository is public. Do not add Vast or R2 credentials there; use a private owner-controlled repository for this path, or configure two private HTTP services instead. Repository writers can read workflow secrets, so the Vast key must have only `misc`, `instance_read`, and `instance_write` scopes. [GitHub's secret guidance](https://docs.github.com/en/actions/reference/security/secure-use) states that anyone with repository write access can read configured secrets.

In that private repository, configure these GitHub Actions secrets only after creating narrowly scoped credentials for this recovery purpose:

- `VAST_BROKER_GITHUB_VAST_API_KEY`
- `VAST_BROKER_R2_BUCKET`
- `VAST_BROKER_R2_ENDPOINT`
- `VAST_BROKER_R2_ACCESS_KEY_ID`
- `VAST_BROKER_R2_SECRET_ACCESS_KEY`
- `VAST_BROKER_R2_ACCOUNT_ID`
- `VAST_BROKER_ALERT_WEBHOOK_URL`
- `VAST_BROKER_ALERT_WEBHOOK_TOKEN` (optional)

GitHub Actions' run delay and missed schedules make it unsuitable as the only backup. Its declared control-host/failure-domain identities must be distinct from the persistent service and initiating machine. GitHub identity strings are declarations; verify actual deployment separation and loss behavior before enabling paid use.

## Initiating broker configuration

Set `VAST_BROKER_GUARDIAN_GATE_FACTORY=vast_broker.guardian_http:guardian_gate_from_environment`. Configure the durable R2 variables above in the initiating control plane and one guardian per slot:

```text
VAST_BROKER_GUARDIAN_1_TYPE=http
VAST_BROKER_GUARDIAN_1_URL=https://guardian.example.net
VAST_BROKER_GUARDIAN_1_TOKEN=<same secret configured on the service>
VAST_BROKER_GUARDIAN_1_ID=<persistent-service-id>
VAST_BROKER_GUARDIAN_1_CONTROL_HOST_ID=<persistent-service-host>
VAST_BROKER_GUARDIAN_1_FAILURE_DOMAIN_ID=<persistent-service-domain>

VAST_BROKER_GUARDIAN_2_TYPE=github_actions
VAST_BROKER_GITHUB_REPOSITORY=<owner/repository>
VAST_BROKER_GITHUB_GUARDIAN_WORKFLOW=vast-guardian.yml
VAST_BROKER_GITHUB_GUARDIAN_ID=github-actions-recovery
VAST_BROKER_GITHUB_CONTROL_HOST_ID=github-hosted-runner
VAST_BROKER_GITHUB_FAILURE_DOMAIN_ID=github-actions
```

For a second persistent HTTP service, set guardian slot 2 to `TYPE=http` and configure its URL, token, ID, control-host ID, and failure-domain ID instead. Never put secret values in a repository, workflow input, CLI argument, or report.

Before any paid run, verify each service can read the same R2 lease, acknowledge and read back the exact fence, recover owned resources using test leases in a non-billing environment or controlled failure-injection setup, alert on unresolved cleanup, and recover after its peer and the initiating host are stopped. Physical failure-domain independence and credential scopes are deployment facts that local unit tests cannot establish. Until those checks are complete, the broker correctly blocks paid creation. No service or Vast instance is deployed by this repository change.
