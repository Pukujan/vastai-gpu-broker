# Open-Jev 9B ran on one RTX 3090 and stopped cleanly

On 2026-09-29, we ran the pinned Open-Jev 9B adapter on one on-demand NVIDIA RTX 3090 with 24 GB of VRAM. The live server loaded Qwen3.5-9B and returned a ready health response. Six inference requests passed during a 184.1-second observation window.

Report recorded at 2026-09-29 15:51 UTC. The source output preserved the run date and duration, but not its exact start time.

## What the run showed

- Rental mode: on-demand, not bidding.
- Host status: unverified; the marketplace reliability value was 0.9936295.
- Displayed instance rate: $0.1886666 per hour. The offer displayed network rates of about $2.67/TB down and $4.00/TB up.
- Loaded service: `Qwen/Qwen3.5-9B`, with the Open-Jev `lora_decision_head` method.
- Inference: `openjev_inference_verified=True`; six successful requests; one measured response took 1.075 seconds. The response SHA-256 was `3a63b32a6030a2a6a5144505b95111d738c36f9f447bbe509eef120cd9eb12a6`.
- Cleanup: the instance was destroyed. Fresh provider inventories reported zero remaining instances and zero volumes.
- Billing: provider charge history was checked after teardown. This public report leaves out account ledger amounts and balances.

The model server listened on `127.0.0.1` inside the rented machine. The challenge did not publish a public inference endpoint. The public record omits the account balance, instance and host identifiers, raw journal, API credentials, and request payload.

## Pinned model sources

The broker recipe pins the [Open-Jev source](https://github.com/Zefan-Cai/Open-Jev/tree/3308a15ccd7eea1df7a37d6ddc39b023b801ba16), the [Open-Jev 9B adapter](https://huggingface.co/ZefanCai/Open-Jev-9B/tree/47e966881e489511c0c7f5633a9e1960a676a551), and the [Qwen3.5-9B base model](https://huggingface.co/Qwen/Qwen3.5-9B/tree/c202236235762e1c871ad0ccb60c8ee5ba337b9a). The live health response reported `{"status":"ready","model":"Qwen/Qwen3.5-9B","method":"lora_decision_head"}`.

## Limits of this result

This result shows that one RTX 3090 host ran this pinned model configuration and that Vast later reported no remaining instance or volume. The selected host was unverified. The result does not establish a minimum GPU requirement, future offer availability, a public service, or recovery by deployed guardians across separate failure domains. The normal paid broker path continues to require those guardians.
