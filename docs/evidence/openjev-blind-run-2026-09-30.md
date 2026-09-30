# Twenty-eight Open-Jev 9B creates on Vast, all cleaned up, the transport gaps pinned down and the 16 GB fit question decided

On 2026-09-30 an owner-authorized direct run tried to rent GPUs for the pinned
Open-Jev 9B trial with the remote guardians parked. Twenty-eight labeled
contracts were created across the day (a morning eight-create phase plus
afternoon SSH-walk attempts). Every instance was destroyed, and every cleanup
ended with fresh complete account inventories showing zero instances and zero
volumes. Session spend is $1.030 against the owner's $2.00 cap (see Money and
cleanup for the timestamped figure). The run pinned down three hard facts: the
command endpoint the recipe relies on is closed to this account; Vast's curated
images do not carry the defaults the pinned recipe assumes; and the pinned
BF16 recipe does not fit any Vast RTX 5060 Ti host observed in this session —
it OOMs at weight placement even with a warm base cache and the allocator
line enabled.
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

SSH works as a transport, with a caveat the afternoon established: auth is
host-specific. Registering an ephemeral account key before create
(`POST /api/v0/ssh`), reading the endpoint from the instance record's
`ssh_host`/`ssh_port`, and logging in as root opened first-try on some hosts
and was denied on every probe attempt (up to 25) on others, with the
identical image, key, and flow — a per-host key-injection lottery, not a
client bug. Delivering the recipe's step bytes over SSH got further than the
command API ever did. The remaining failures were image defaults:
`vastai/pytorch:cuda-13.2.1-auto` has git, nvidia-smi and curl but no `python`
alias, and `vastai/pytorch:cuda-12.8.1-auto` has no `/workspace`, which the
pinned recipe writes its logs to and probes with `df -BG /workspace`. The rented
GPU itself was right every time the check ran — one RTX 5060 Ti at 16311 MiB.
The `@vastai-automatic-tag` resolves to different CUDA builds on different
machines, so pinning a recipe to it means pinning to a moving target.

## Money and cleanup

Actual machine spend is $1.030 by Vast's `GET /api/v0/charges` (day range
2026-09-28 to 2026-09-30, `select_filters` with unix seconds) across the
28 labeled contracts at 20:18 UTC: $0.866 GPU, $0.133 disk, $0.031 bandwidth.
The figure grew through the day as attempts accumulated: the morning
blind-run phase (eight creates) accounted for roughly $0.21, and GPU spend
stood at $0.746 (26 contracts) at 18:55 UTC. The single largest charge,
$0.267, is one host that stalled and I left past a deadline; every later
rental closed in minutes. No volume was ever left behind. After each destroy
the provider's complete listings returned zero instances and zero volumes,
re-checked twice on the last run. Both registered SSH keys were deleted.
No key, host, signed URL, raw journal, or account identifier appears in this file
or the linked comments.

## What the run proved, and the real blocker

The parts the broker owns worked every time. Live search with the documented
constraint names (the issue #23 fix on `main`), the all-in cost gate from the
router's own formula, verification and network-tier filtering, bid and on-demand
rental, journal-before-create, teardown by owned ids, and absence verification all
held across every attempt. SSH carried the recipe's bytes unchanged; its
first-try success was host-dependent, not flow-dependent (see above).

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

The follow-up test then closed the last open hypothesis. On a verified
on-demand host (16311 MiB preflight, 15854 MiB free at the allocator line,
`runtime-install` through `model-start` all `VBR_EXIT_CODE=0`, the Qwen base
preloaded into the default cache to completion before launch, and
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` appended to the env file):
the server still died at the identical point —
`torch.OutOfMemoryError: Tried to allocate 96.00 MiB. GPU 0 has a total
capacity of 15.51 GiB of which 64.00 MiB is free ... 1.14 MiB is reserved but
unallocated`, inside `transformers` `_materialize_copy` during weight
placement. With a warm cache and anti-fragmentation both active, the pinned
BF16 device_map needs roughly 15.5 GiB of a 15.5 GiB-usable card and misses
the last 96 MiB allocation. Across two different hosts the resident footprint
reproduced at 15.41-15.42 GiB. The conclusion the earlier retraction asked
for is now evidence-backed: on Vast 5060 Ti hosts the pinned profile does not
reach health, and no transport or cache fix changes that. The remaining
levers — an accepted 24 GB card, a device_map CPU-offload change, or a
quantized tier — are owner decisions outside this authorization;
`_validate_gpu_preflight` (openjev.py:549-557) stays unchanged so the trial
stays the pinned trial.
