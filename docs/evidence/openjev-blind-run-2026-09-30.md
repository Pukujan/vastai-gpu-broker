# Eight Open-Jev 9B creates on Vast, all cleaned up, the transport and image gaps pinned down

On 2026-09-30 an owner-authorized direct run tried to rent GPUs for the pinned
Open-Jev 9B trial with the remote guardians parked. The morning phase made
eight create calls and seven instances; afternoon SSH-walk attempts added more
(26 labeled contracts in total by 18:55 UTC). Every instance was destroyed, and
every cleanup ended with fresh complete account inventories showing zero
instances and zero volumes. Session spend is $0.863 against the owner's $2.00
cap (see Money and cleanup for the timestamped figure). The run pinned down
two hard facts about Vast's current API: the command endpoint the recipe
relies on is closed to this account, and Vast's curated images do not carry
the defaults the pinned recipe assumes.
[Issue #28](https://github.com/Pukujan/vastai-gpu-broker/issues/28) carries the
defects; the operator comments on
[issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1) carry the
per-attempt detail.

## Owner envelope

- Remote recovery guardians parked by owner decision of 2026-09-29/30. This is
  a direct parked run, not a guardian-verified broker `create`. The host-side
  backstop was a detached local watchdog armed before each create; it shares
  the control machine's fate.
- Caps: $2.00 total. The all-in ceiling started at $0.20 per hour; finding zero
  qualifying offers, the owner raised it to $0.28 per hour on 2026-09-30,
  computed with the router's own cost formula (runtime 7200 s, startup 360 s,
  network allowance $0.05). The $3.00 per TB network hard ceiling never
  changed.
- Every create used a fresh market search; no reused snapshot. Each create
  carried a unique label so the watchdog could find it by ownership.

## What the attempts showed

The execute API is closed to this account. Instances created with the recipe's
own create body (digest or tag image, `runtype: "args"`) reached `running` and
then rejected every `PUT /instances/command/{id}` call with
`invalid_args / Invalid command given.` — 45 of 46 captured bodies on one host,
and again on a second host and partition. A booted, running instance in Vast's
default agent-injected mode on Vast's own curated image (`vastai/pytorch`)
refused the command endpoint the same way, 20 of 20 warmups. Vast's create body
also now requires an explicit `image`; the recipe's params without one were
rejected at the door (`image must be a str, not NoneType`). Vast's one
differently-worded rejection pointed at the fix: "Use ssh to run commands on
running instances."

SSH works as a transport. Registering an ephemeral account key before create
(`POST /api/v0/ssh`), reading the endpoint from the instance record's
`ssh_host`/`ssh_port`, and logging in as root opened on the first try on both
SSH instances. Delivering the recipe's step bytes over SSH got further than the
command API ever did. The remaining failures were image defaults:
`vastai/pytorch:cuda-13.2.1-auto` has git, nvidia-smi and curl but no `python`
alias, and `vastai/pytorch:cuda-12.8.1-auto` has no `/workspace`, which the
pinned recipe writes its logs to and probes with `df -BG /workspace`. The rented
GPU itself was right every time the check ran — one RTX 5060 Ti at 16311 MiB.
The `@vastai-automatic-tag` resolves to different CUDA builds on different
machines, so pinning a recipe to it means pinning to a moving target.

## Money and cleanup

Actual machine spend is $0.863 by Vast's `GET /api/v0/charges` (day range
2026-09-28 to 2026-09-30, `select_filters` with unix seconds) across the
labeled contracts at 18:55 UTC: $0.746 GPU, $0.117 disk, $0.000 bandwidth.
The morning blind-run phase accounted for about $0.21 of that. The single
largest charge, $0.267, is one host that stalled and I left past a deadline;
every later rental closed in minutes. No volume was ever left behind. After
each destroy the provider's complete listings returned zero instances and zero
volumes, re-checked twice on the last run. Both registered SSH keys were deleted.
No key, host, signed URL, raw journal, or account identifier appears in this file
or the linked comments.

## What the run proved, and the real blocker

The parts the broker owns worked every time. Live search with the documented
constraint names (the issue #23 fix on `main`), the all-in cost gate from the
router's own formula, verification and network-tier filtering, bid and on-demand
rental, journal-before-create, teardown by owned ids, and absence verification all
held across every attempt. SSH opened first-try with an account key registered
before create, and the recipe's bytes ran unchanged over it.

The deepest run so far carried `gpu-preflight` through `model-start` to
`VBR_EXIT_CODE=0` with the ~18 GB adapter downloaded, and the server died
loading weights: `torch.OutOfMemoryError: CUDA out of memory … GPU 0 has a
total capacity of 15.48 GiB of which 52.31 MiB is free`, at
`tensor.to(device=device, dtype=dtype)` during placement. An earlier version of
this section called that a deterministic contradiction between the pinned BF16
`JEV_DEVICE_MAP` (openjev.py:133-143) and the 16 GB tier. That claim is
retracted — see [issue #28
comment](https://github.com/Pukujan/vastai-gpu-broker/issues/28#issuecomment-5916098705)
for the full reasoning. What the evidence supports:

- The miss is tens of MiB (15.42 GiB resident of 15.48 GiB PyTorch-visible),
  not the multi-gigabyte gap a "18 GB checkpoint cannot fit" story needs.
- Only 5.14 MiB was reserved-but-unallocated, so allocator fragmentation has
  almost nothing to give back; `expandable_segments` is not the rescue.
- That OOM happened while the Qwen base was still `Fetching 4 files: 25%`, so
  a warm default-cache preload before `model-start` was never actually tested
  against a load that reached placement.
- Upstream's `docs/consumer-gpu.md` note (14.78 GiB resident, 2.2 GiB free on
  a Linux RTX 5060 Ti 16 GB) is an author report on the author's hardware; it
  is not a headroom guarantee on Vast hosts, where the driver reserve leaves
  15.48 GiB visible under the 16311 MiB the preflight reports.

Still true and pinned: the broker's own parts worked every time (live search
with the documented constraint names, the router's all-in cost gate,
verification and network-tier filtering, journal-before-create, teardown and
absence proof), the execute API is closed to this account so SSH carries the
identical step bytes, and the curated image tag is a moving target that must
provably carry `python` and `/workspace`. A follow-up attempt (on-demand host,
full-16 GiB preflight floor, warm base preload as a logged deviation, detached
per-step polling so transport churn cannot fake a step failure) is the active
test. If the pinned bytes still OOM at placement with the base cached, the
remaining levers — an accepted 24 GB card, a device_map CPU-offload change, or
a quantized tier — are owner decisions outside this authorization, and
`_validate_gpu_preflight` (openjev.py:549-557) stays unchanged so the trial
stays the pinned trial.
