# Local use and Vast API references

The broker runs on your own computer; model inference still runs on the rented Vast GPU. Other projects can install it from this public repository and import the same Python interface, while the official Vast documentation and SDK remain available for provider-level reference.

## Install the broker locally

From a checkout of this repository, install the package in your active Python environment:

```powershell
python -m pip install -e .
vast-broker --help
```

To start from another project, clone this repository or install a reviewed commit directly:

```powershell
python -m pip install "git+https://github.com/Pukujan/vastai-gpu-broker.git@<reviewed-commit-sha>"
```

Replace `<reviewed-commit-sha>` with the commit you have inspected. The package requires Python 3.11 or newer and has no runtime third-party dependencies.

## Find Vast's API, CLI, and SDK

- [Vast REST API reference](https://docs.vast.ai/api-reference/search/search-offers) — the API base is `https://console.vast.ai/api/v0`. Direct references: [search offers](https://docs.vast.ai/api-reference/search/search-offers), [create instance](https://docs.vast.ai/api-reference/instances/create-instance), [show instances](https://docs.vast.ai/api-reference/instances/show-instances), and [destroy instance](https://docs.vast.ai/api-reference/instances/destroy-instance). Offer search uses `POST /bundles` under that base.
- [Official Vast Python SDK and CLI](https://github.com/vast-ai/vast-cli) — install on Windows with `python -m pip install vastai`. The package provides the `vastai` command and Python SDK.
- [Vast CLI quickstart](https://docs.vast.ai/cli/hello-world) — provider setup and direct CLI examples.

The direct Vast API, SDK, and CLI can create resources without this broker's routing, spend checks, ownership journal, or cleanup verification. Use them for provider reference or deliberate administration; use the broker entry points below when a rental must follow this repository's controls.

## Search and manage through the broker

Set credentials through a local secret manager or your process environment. Do not place keys in source files, command arguments, shell history, or reports.

- `VAST_BROKER_SEARCH_API_KEY` is for live offer search and should have the Vast `misc` permission.
- `VAST_BROKER_LEASE_API_KEY` is for instance lifecycle operations and should have only `misc`, `instance_read`, and `instance_write`.
- `VAST_BROKER_JOURNAL_DIR` points to a private local directory for durable lease ownership records.

The read-only search works from a local checkout:

```powershell
vast-broker search --disk-gb 100 --output current-offers.json
```

The public Python facade is `VastRentaiService`:

```python
from vast_broker import LeaseJournal, VastOffersClient, VastRentaiService

service = VastRentaiService(
    provider=VastOffersClient(api_key=lease_key),
    search_client=VastOffersClient(api_key=read_key),
    journal=LeaseJournal(private_journal_dir),
    guardian_gate=ready_two_guardian_gate,
)
offers = service.search(filters, disk_gb=allocated_disk_gb)
```

The facade also exposes guarded `create(proposal, confirmation_digest, ...)`, `status(request_id)`, and `destroy(request_id)`. `create` needs a fresh router proposal, exact plan bindings, bounded operation code, owner limits, and a ready two-guardian gate. `status` and `destroy` work only with the request's private broker journal. The CLI's `create` command is the pinned Open-Jev 9B trial, not a generic raw-offer rental command. See the [main usage guide](../README.md#one-open-jev-9b-trial), [credential policy](agent-credential-access.md), and [official provider notes](official-docs.md).

The live Open-Jev challenge is recorded separately in [the sanitized run report](evidence/openjev-9b-live-run-2026-09-29.md). It is evidence for one completed run and teardown; it does not replace the guardian requirement for the broker's normal paid path.
