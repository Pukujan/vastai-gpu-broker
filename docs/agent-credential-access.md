# Agent access and Vast API key custody

## Decision

Use this repository as the durable instruction set and offer cache. Prefer keeping the Vast API key in one central vault and injecting it only when the agent's work actually starts. For paid GPU operations, the intended interface remains a user-controlled broker/MCP that exposes constrained operations, not an unrestricted key. A multi-computer credential-fetch mechanism may use a managed vault; do not build a custom secret-storage service until a standard vault proves insufficient.

This repository currently implements none of the vault, interactive approval, or broker/MCP integrations; it is listing-only. Do not make instance creation available merely by adding a key to an agent's project `.env`.

## Interactive approval and queue delay

Phone approval or an authenticator can gate a vault access, but trigger the challenge **when the task is running and actually needs the secret**, not when it is queued or assigned. A task may wait in a queue for hours; the approval grant/token lifetime starts after approval. If the agent does not use it before its expiry, it should request a new approval. Do not try to mint a short-lived token before queueing and expect it to survive.

Email codes/links work operationally if the user approves after the request begins, but email is usually a weaker approval channel than a device-bound passkey or phone confirmation. An authenticator code is short-lived and can expire while a human is reading/responding; have the agent ask again after it begins, and let it renew/retry rather than embedding a TOTP code in the repo or long-lived task instructions.

The approval token lifetime and the Vast API key lifetime are different things. The Vast key remains valid until reset/deletion; its current docs describe scoped keys, reset, and delete, but do not describe per-key expiry. An agent that writes the key to a persistent project `.env` can continue to use it after an approval session ends. Prefer fetching/injecting it into the requested process only. If a task truly must save it to `.env`, use an ignored, local file and accept that the agent/workspace can read it; approval does not erase that copy.

## Why not a password-encrypted key in this repo?

Encryption at rest can be useful when the decryption secret is stored separately. A strong randomly generated passphrase plus a standard tool such as `age` can protect a ciphertext file from a reader who has only the repository. But if the agent receives that passphrase in the same chat/task transcript, the agent can decrypt the key. The passphrase then protects the repository from other readers who lack the transcript; it does not restrict the intended agent or make the decrypted credential safe from that agent's code/tools.

Putting the plaintext key in another project's `.env` makes it available to code and agents with access to that project. `.gitignore` reduces accidental commits but is not an access-control boundary. A malicious or compromised tool, dependency, prompt-injected instruction, or workflow could read and send it elsewhere. The key must never be printed, logged, committed, placed in shell history, or passed as a command-line argument.

If a later deployment truly needs decryption, use a separately reviewed standard encryption tool, prompt for the passphrase through a hidden terminal input, decrypt to process memory/pipe or a user-owned credential store, and avoid writing a plaintext project `.env`. If writing a local `.env` is unavoidable, verify it is ignored before writing and set restrictive OS ACLs; this remains weaker than keeping the key out of the agent workspace.

## Intended local broker/MCP boundary

The server, not repository instructions or model behavior, must enforce these controls:

- Hold a dedicated Vast key outside the workspace. Offer sync uses the narrowest key available for offer search (`misc`); paid lifecycle uses a separate key with only `misc`, `instance_read`, and `instance_write`, and no billing-write permission.
- Do not return the Vast key from any tool. Do not accept user-provided credentials in tool arguments.
- Expose read-only tools separately from paid tools: `search_offers`, `show_offer_quote`, `list_broker_leases`, `request_lease`, and `destroy_lease`.
- `request_lease` validates the exact documented model profile, simultaneous workload fit, selected live offer, hourly cap, total spend/runtime cap, disk amount, and supported runtime configuration. Reject missing or unknown evidence. Never allow free-form API payloads or arbitrary templates to bypass validation.
- Before create, show a quote and require a separate explicit human approval in the trusted client. The server independently checks the approved offer ID and caps; a sentence in `AGENTS.md` is not a payment control.
- Persist the returned instance ID and absolute deadline before giving the agent connection details. Set an independent watchdog and reconcile leases after restart. `destroy_lease` should be easy and safe to call repeatedly. `stop` is not cleanup because Vast storage continues billing.
- Log request ID, model profile revision, offer ID, quote, approved caps, instance ID, and teardown outcome, but never the key or secret-bearing environment.
- Bind a local stdio server to the trusted desktop agent, or if a remote server is ever necessary, authenticate and authorize every caller. Do not expose an unauthenticated HTTP endpoint. Audit server code before enabling it: a local MCP server is trusted executable code and has access to its configured secrets.

MCP is the transport/integration mechanism, not the policy boundary by itself. The tool implementation must validate all arguments and enforce the spend/lifecycle rules even if an agent is confused by untrusted offer metadata or model text.

## Current options

| Option | Key visible to agent? | Suitability |
|---|---|---|
| Local broker/MCP on the trusted desktop | No; only validated operations/results | Best for paid GPU actions when agents run on that configured computer. It must enforce caps itself. |
| Managed secrets vault + CLI injection | Agent process can read the value; it need not be written to project `.env` | Good cross-computer path. Authenticate each computer once, or prompt for user approval on demand. A process receiving the key can use/exfiltrate it. |
| Private Google Drive file + encrypted payload | Yes, after decryption; Drive login and passphrase are separate checks | Lowest-friction bridge if every trusted computer already has Drive. Encrypt the **file contents**, keep Drive sharing restricted, and retrieve only after the task starts. An encrypted URL alone does not protect the credential. |
| GitHub Actions offer-sync secret | No to ordinary repository readers, but available to workflows; repository writers can change workflows and use secrets | Acceptable only for a dedicated scoped search key, private owner-controlled repo, and audited workflow. Never use a full-access key. |
| Encrypted key file + chat passphrase | Yes, after decryption; transcript conveys the unlock secret | Only protects ciphertext from repo-only readers. Not recommended as the normal retrieval path. |
| Plaintext key in agent's project `.env` | Yes, to that project's code/tools | Do not use as default. If required for compatibility, retrieve on demand, verify ignored/local-only storage, and use a narrowly scoped key. |

## Free hosted vaults considered

- **Infisical Cloud Free** currently advertises $0, up to five human/machine identities, unlimited projects, 2FA, and static-secret Agent Proxy. Its current plan comparison puts access controls, rotation, secret versioning, and audit-log retention in paid tiers. Its `infisical run` command injects secrets into a process without creating a project `.env`; the user still needs to authenticate on each computer, and code running as that process can read the value. This is a plausible trial for centralized `.env` friction, but not an approval gate with free granular per-agent permissions.
- **Bitwarden Free** supports a personal vault across devices and the CLI can retrieve vault items after the vault is unlocked. This is a good personal password-manager store; it does not by itself keep the decrypted key from an agent that is asked to run `bw get`.
- **Google Cloud Secret Manager** currently has an always-free allowance of six active secret versions and 10,000 access operations per billing account per month. It requires GCP identity/authorization setup on every machine; a long-lived service credential would recreate the bootstrap problem.
- A private Google Drive encrypted file is a workable bridge for a single owner already signed in on all trusted machines, but Drive is storage, not a secret broker. A connector that returns plaintext file content may put that secret into the agent's context. Use a local decrypt-to-file/process workflow if choosing this option.

For the user's current need, start by evaluating a managed vault CLI such as Infisical Free against the current Google Drive workflow; don't build a self-hosted secret API yet. A self-hosted service needs the same authentication, authorization, rotation, logging, backups, and encryption controls and would itself need a durable master secret. If the actual requirement becomes "agent calls Vast but must never see the API key," use a broker/proxy rather than a fetch-the-key vault.

If an agent runs on a separate cloud/hosted environment that cannot call the local broker, it should not automatically fall back to receiving the key. Use an authenticated, narrowly scoped broker service with server-side caps and audit, or keep the paid action in a trusted local agent.

## References

- [Vast API keys](https://docs.vast.ai/guides/reference/api-keys) and [Vast permission categories](https://docs.vast.ai/api-reference/permissions)
- [Infisical pricing](https://infisical.com/pricing), [`infisical run`](https://infisical.com/docs/cli/commands/run), and [machine identity overview](https://infisical.com/docs/documentation/platform/identities/overview)
- [Bitwarden CLI](https://bitwarden.com/help/cli/) and [free plan](https://bitwarden.com/help/password-manager-plans/)
- [Google Cloud Secret Manager pricing](https://cloud.google.com/secret-manager/pricing)
- [GitHub secure use for Actions](https://docs.github.com/en/actions/reference/security/secure-use) — repository writers can use configured secrets; minimize credential permissions.
- [MCP security policy](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/SECURITY.md) — local servers are trusted software with the access of their execution environment, and the server operator must validate inputs and enforce access control.
- [Vast lifecycle documentation](https://docs.vast.ai/guides/instances/pricing) — compute/storage charges and destroy behavior; see [`official-docs.md`](official-docs.md).
