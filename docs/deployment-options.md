# Repository visibility and hosting options

## Repository visibility

`vastai-gpu-broker` is public by owner choice. Its source, documentation, issue tracker, and any committed offer snapshots are visible to everyone. Do not commit API keys, encrypted secret payloads, private Drive links, account identifiers, lease journals, or private account data. Public visibility does not make a repository an appropriate secret store.

There is no separate vault repository or vault service in this project today. This repository is the Vast marketplace/broker project; it is public, while any future secret broker must remain separately authenticated and private.

## Can the service cold-start?

Yes. Separate the small control service from durable secret storage. A stateless API can scale to zero and start on its first authenticated request; the first call may wait for a cold start. Put secrets in a managed vault, not in the service's temporary filesystem.

| Option | Cold-start behavior | Fit and limits |
|---|---|---|
| Managed vault CLI (for example, Infisical Cloud) | Vault is already hosted; each agent fetches on demand | Simplest first trial. No service to operate. Agents still need an authenticated login/bootstrap on each computer. |
| User's PC behind a private network | No response while the PC is asleep/offline; request-triggered wake is hardware/router dependent | Keeps control local, but remote access and availability need extra setup. Do not expose an unauthenticated port to the public internet. |
| Google Cloud Run + Secret Manager | Cloud Run scales to zero and can start on an authenticated request | Technically suitable for a small stateless broker. Secret Manager provides the durable secret store. Configure caller authentication, a maximum instance count, and billing alerts; the free tier is an allowance, not a hard spending cap. Google account/Cloud IAM access must be set up separately from a Codex Google Drive connection. |
| Render Free web service | Spins down after 15 minutes idle and takes about a minute to restart | Not suitable as the only secret store: its local filesystem is ephemeral and changes are lost on spin-down/restart/redeploy. Free Postgres also expires after 30 days. |

For any remotely hosted secret broker, require caller authentication on every request, use TLS, store keys only in a managed secret store, and enforce authorization and paid-operation limits server-side. Keep the service private/authenticated; a random URL or encrypted URL is not an authentication system. A task queue delay is handled by starting the approval challenge after the task begins, then issuing a fresh access grant.

## Current recommendation

Do not build or host a custom all-keys vault yet. Pilot a managed vault CLI first; it avoids another service to deploy and secure. If a remote broker becomes necessary for agents on several computers, use an authenticated scale-to-zero host with a managed secret store and a local approval flow. Keep that service and its secret store private; keep this public repository limited to source, public documentation, and deliberately public marketplace snapshots.

## Official hosting references

- [Cloud Run autoscaling and scale-to-zero](https://cloud.google.com/run/docs/about-instance-autoscaling)
- [Cloud Run pricing and free tier](https://cloud.google.com/run/pricing)
- [Google Secret Manager pricing and free tier](https://cloud.google.com/secret-manager/pricing)
- [Render free services: spin-down, cold-start, and ephemeral filesystem](https://render.com/docs/free)
- [Infisical Free plan](https://infisical.com/pricing)
