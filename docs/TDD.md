# Test-driven development and acceptance contract

Tests must be written against [SPEC.md](../SPEC.md), [PDD.md](PDD.md), and [SDD.md](SDD.md). The suite must observe paid-call traces, state transitions, artifacts, and independent fake-provider truth rather than merely repeat the implementation's helper functions.

## Required test layers

1. Pure contract tests for request/evidence validation, unit conversion, cost accounting, eligibility/ranking, and finite bidding.
2. Stateful integration tests with a fake Vast provider, fake remote runtime, controllable clocks, durable temporary journals, and injected process/network/storage failures. The fake provider maintains its own instance set independently of the controller journal.
3. Metamorphic tests that transform a valid scenario and check required invariant changes.
4. A separate private holdout evaluator whose cases and expected traces are unavailable to implementation builders and the challenge agent.
5. One owner-authorized real paid task from a clean checkout after release, with actual inference and verified cleanup. Offline/fake tests never substitute for that run.

## Acceptance cases

| Test ID | Required observation | Properties |
|---|---|---|
| A01 | A task containing only an unresolved model query receives exact-model research tasks and makes zero create calls. | P01, P04 |
| A02 | Missing publisher hardware numbers route to external evidence research; invented numeric values or unrelated deployment reports do not pass. | P02, P03 |
| A03 | An external tested configuration is usable only within its provenance/profile scope; it never becomes a publisher requirement or proven minimum. | P02, P03 |
| A04 | An adapter with a required pinned base and custom API cannot be planned as a standalone chat model. | P04, P16 |
| A05 | Two-model concurrent request with only individual-model evidence remains unconfirmed or explicitly enters bounded combined validation. | P05 |
| A06 | Missing, negative, nonfinite, conflicting, or expired authorization/caps cause zero paid calls; source-backed owner policy is exposed when used. | P09 |
| A07 | Offer responses preserve every verification category and rental type; unsupported/prepaid choices have explicit exclusion reasons. | P07 |
| A08 | A lower compute price with expensive requested storage/known network transfer loses to lower total planned cost; zero-cost and unknown-cost fields remain distinct. | P08 |
| A09 | USD, bytes, per-TB rates, storage units and machine-vs-GPU bids are converted without hidden scaling or double counting. | P08, P09 |
| A10 | Stale caches, saturated queries, changed evidence/recipe/caps, price changes and vanished offers require fresh revalidation; incomplete results cannot claim a global cheapest offer. | P10 |
| A11 | Interruptible bid creation and changes remain below all caps; attempts/time are finite; highest bid does not bypass a fake on-demand occupant. | P09, P11 |
| A12 | A bid accepted but never running destroys its instance at the start deadline; paused time still consumes storage/runtime envelope. | P08, P11, P14 |
| A13 | A successful remote create with lost HTTP response does not permit another create; reconciliation finds and owns or destroys the labeled instance. | P12, P15 |
| A14 | Failed journal persistence/supervisor startup before create makes zero create calls. Post-create persistence failure triggers cleanup and a failed result. | P13, P14 |
| A15 | Killing the main controller does not stop the independent supervisor from attempting deadline destruction; restart reconciles outstanding IDs. | P13, P15 |
| A16 | Install failure, OOM, disk-full, timeout, cancellation, wrong model identity, malformed outputs and failed health probes all cause destruction. | P14, P16 |
| A17 | Delete acknowledged while instance remains listed is not success. Timeout/401/403/malformed listing is not proof of deletion. Cleanup retries remain journaled. | P14, P17 |
| A18 | In sequential mode two concurrent callers for the same request cannot create two leases; an authorized race has one operation and only its reserved attempts. Cleanup operates only on request-owned IDs, preserving unrelated user instances. | P12, P15, P38 |
| A19 | Sentinel credentials embedded in transport errors, subprocess output, request env or traces are absent from public reports/logs/repo artifacts. | P18 |
| A20 | Import API and CLI reject the same malformed request and follow the same lifecycle decisions. | P19 |
| A21 | A checkpoint claiming readiness without the required tests/real run is rejected by checkpoint validation. | P20 |
| A22 | An unresolved artifact routes through researched exact variants/quantizations/benchmarks to selection; absent selection permission makes zero paid calls. Exact artifact and valid scoped authorization are traced and skip another model question. | P21 |
| A23 | A cold/idle/hung-request deadline causes the configured stop/destroy stages through the independent supervisor. Agent heartbeat and health polling do not reset idle; bounded retention ends in verified destruction. | P22, P14 |
| A24 | Ambiguous-family research compares exact IDs/revisions/variants, per-variant source scope and live suitable-offer count/min/median/mean by type with disk/network components. It makes zero create calls before exact selection. | P23, P21 |
| A25 | An ambiguous family discovers publisher/base and relevant community quantizer artifact/format candidates using generic fake-Hub records. It presents candidate-scoped requirements/quality/runtime evidence and Vast statistics, then requests exact selection rather than silently choosing. | P21, P23 |
| A26 | A request identifying an official base without specifying its artifact/quantization/provider researches that base and relevant permitted community artifacts and requests the open artifact choice. An explicit publisher-only restriction excludes community substitution. | P24, P21 |
| A27 | An exact repository/URL plus artifact name resolves that file set/revision, researches only that candidate and dependencies, and reaches the final cost gate with no model/alternative menu. Missing numeric caps return cost-input reasons only. | P25, P09 |
| A28 | Explicit permission to select any compatible artifact within a named base/4-bit scope selects by the authorized objective without asking for candidate confirmation; wrong base/bit depth/provider/format or unsupported candidates stay outside scope. Numeric cap/evidence/lifecycle gates remain mandatory. | P26, P09 |
| A29 | Missing exact artifact/no candidate/unresolved identity exposes distinct reason codes and an alternatives-question decision. No unauthorized substitute is chosen. Transient retrieval errors remain research retry states, not artifact-absence or payment authorization. | P27, P01 |
| A30 | Changing model/format/provider/quantization beyond selection scope invalidates its receipt. Replanning another eligible artifact within unchanged auto-selection scope requires no redundant model confirmation, but still revalidates evidence and live quote. | P26, P10 |
| A31 | Absent publisher numeric requirements triggers config/architecture/artifact/runtime research and emits actual unique artifact disk bytes plus separate sourced weight/cache/loading/activation/runtime terms. Missing terms remain unknown; a lower bound alone cannot pass fit. | P28, P03 |
| A32 | Synthetic dense and MoE configs with equal active parameters but different total expert tensors produce distinct stored/resident weight terms. MoE active count cannot stand in for all resident experts; documented expert/CPU placement is accounted per device and RAM. | P29, P28 |
| A33 | A source-documented conventional GQA cache uses KV heads and declared context/concurrency/dtype. Hybrid/recurrent/MLA/sliding/paged layouts require their own sourced rules or remain unknown; generic formulas cannot silently pass. | P30, P28 |
| A34 | Quantized artifact bytes remain distinct from loaded resident weight/metadata/dequantization/temporary overhead. Missing runtime quant allocation documentation keeps consequential terms unknown; measured artifact facts and formula estimates retain distinct provenance. | P28, P03 |
| A35 | With an established rate basis, both directions obey current owner tiers at zero, below/exactly/above $1, $2 and $3/TB. Exactly $1 is outside preference, exactly $2 is expensive, exactly $3 remains within cap, and either direction above $3 rejects. Old workspace tiers cannot override the policy receipt. | P31, P09 |
| A36 | Raw per-GB, native per-TB and CLI display values retain distinct source scope. Conflicting directional fields, missing byte/month divisors and unsupported 1000/1024/GB/GiB/730 conversions remain unresolved and cannot authorize a paid call that depends on that inference; an independently sourced exact quote remains usable. Both valid zero USD rates remain zero independently of an unknown byte divisor. | P32, P03 |
| A37 | Public planning emits every startup/download/install/load/warmup/benchmark/runtime/interruption/retention/cleanup phase and directional traffic category. It counts the provider total once, adds only separate contracts/phases, preserves unknowns, and ranks lower complete cost above cheaper compute. | P33, P08 |
| A38 | Search and create bind exact allocated container/volume quantities and quote digests. Allocation changes invalidate planning; file deletion does not reduce fixed allocation cost; a monthly rate without an evidenced divisor cannot supply exact hourly storage. | P33, P32, P10 |
| A39 | Cache planning compares measured fresh and warm time/bytes/cost, reuse count/window and host premium against retained storage/copy cost. Missing terms produce unknown break-even; absent/expired/wrong-revision/quant/runtime caches cannot reduce known download exposure. | P34, P03 |
| A40 | The fake provider binds a fixed local volume to one machine, bills it after instance destruction, rejects cross-host attachment and deletion while attached, and retains its cleanup obligation through the explicit deletion deadline. Instance absence alone cannot pass full teardown. | P34, P14, P39 |
| A41 | Paid run preparation binds a portable harness/image/target/dataset/probe receipt before create. Missing preparation routes to unpaid preparation; GPU-specific work is budgeted. PC/Linux/Mac names and old notes cannot supply invented specifications, uptime or free monetary cost. | P35, P33 |
| A42 | Engine, localhost HTTP, Tailscale clients and isolated checker runs produce separately scoped receipts. Synthetic WAN delay/path changes alter client results without rewriting host metrics; cache/runtime/workload/concurrency changes invalidate matched evidence; saturated clients/colocated checkers preserve limitations. | P35, P10 |
| A43 | Serialized Unicode request/response bytes, directional interface-counter deltas and provider usage/charges remain separate. Retries, headers/transport overhead, unrelated scoped traffic and reset/wrap cases cannot undercount or become token-derived exact billing; missing/rounded/delayed observations remain explicit. | P36, P33 |
| A44 | Missing aggregate race limits create nothing. Atomic reservations cover all contenders starting/downloading together, bid escalation, storage/retention, cleanup, maximum instances/GPUs and startup deadline; dispatch/escalation never exceeds the authorized aggregate envelope. | P37, P09, P11 |
| A45 | Concurrent PC/Linux/Mac callers with one ID and identical digest attach to one fenced operation; sequential has one unresolved attempt and a race only its finite reservations. Changed digests conflict; crashes/lost responses reconcile the same attempt without replacement or duplicate operation. | P12, P38, P15 |
| A46 | A winning contender passes full hardware/model-set acceptance. Started/unclear losers and separate volumes are destroyed and verified absent; acknowledgement/partial pagination/auth failure/lingering volume keeps reservations and `CLEANUP_PENDING`. No new dispatch/escalation or single-contract/zero-cost success claim follows uncertain cleanup. | P39, P14, P17, P37 |
| A47 | Two individually successful models that cannot remain resident concurrently cannot satisfy combined acceptance or win. A genuine combined probe records complete set identity, placement, concurrent workload and measured scope; an explicit switching request is evaluated as a different workload. | P05, P16, P39 |
| A48 | Start urgency and inference-throughput/latency requirements are parsed separately. Exact user-selected models and simultaneous/sequential mode are preserved; eligible routes meet the explicit deadline/performance target and rank by the authorized objective. `run it` authorizes only the matching proposal digest and caps; stale/changed quotes require re-estimation. Missing policy mappings for vague urgency block or request input. | P40, P09, P10 |
| A49 | The supervisor is started and deadline-armed before the fake create call. On benchmark completion, the result and artifact are durably retrievable before destroy begins or finishes; delayed, denied, or failed deletion leaves a separate `CLEANUP_PENDING` receipt/update without hiding or changing the benchmark result. Later verified absence emits a cleanup receipt; benchmark/install failure still triggers the same cleanup path. | P41, P13, P14, P17 |
| A50 | Kill the child supervisor while the initiating host stays up: its heartbeat is detected as stale, recovery restarts/reconciles it, and request-owned resources are destroyed and verified or remain visibly `CLEANUP_PENDING`; no unrelated instance is touched. | P42, P14, P15 |
| A51 | Kill the initiating host and child supervisor, then recover the separate guardian: it reads the durable fenced lease, reconciles ambiguous creates and retained volumes, retries cleanup, and alerts on unresolved IDs. Simulate guardian/API/network failure too: the system must not claim cleanup, zero ongoing cost, or an absolute spend ceiling; it must retain the obligation and report the failure domain. | P42, P14, P15, P17 |
| A52 | Simulate Vast credit reaching zero with no saved card and with a saved card. Provider behavior must not satisfy the broker cleanup obligation: the broker still requests and verifies destruction, preserves disk/grace/autobilling uncertainty, and never calls balance depletion a cleanup trigger or exact cap. | P43, P44, P09, P14, P17 |
| A53 | Complete, fail, or cancel a run and verify the broker automatically requests destruction; kill the agent/controller/watchdog at each lifecycle phase and verify the independent guardian detects lease/heartbeat expiry, destroys only that request's resources, and confirms absence or reports `CLEANUP_PENDING`. No manual close action or account-balance exhaustion is required. | P41, P42, P44, P14, P15 |
| A54 | Before create, require acknowledgements from two cleanup paths in separate control-host failure domains and a durable fenced lease record both can read. Kill the agent, initiating controller, and local child supervisor together; the remote guardian must reconcile an ambiguous create, destroy only owned instances/volumes, verify absence, and retain an alert/receipt. Missing guardian or registry acknowledgement makes zero creates. | P13, P14, P15, P42, P45 |
| A55 | Stop either guardian, the shared registry, its credentials, the network, or Vast in turn. The surviving guardian retries and reports the failure; if every control path or the registry is unavailable, keep cleanup pending and make no bounded-deletion or hard-final-bill claim. Restore service and verify idempotent recovery without duplicate creates or unrelated deletion. | P42, P44, P45, P17 |
| A56 | Present a logged-in local Chrome/CDP session, Jev browser automation, or a scheduled GitHub workflow as the only backup. The pre-create gate must reject it as a recovery guardian. Jev may assist an interactive task, but loss of its browser process or host cannot be counted as cleanup coverage. | P42, P45, P09 |
| A57 | Apply the owner's hourly cap to GPU, startup, and authorized network exposure together. An offer whose machine quote alone fits but whose all-in rate exceeds the cap is blocked; an all-in eligible offer remains subject to the total cap. | P08, P09 |
| A58 | Complete, fail, cancel, or expire a lease and verify the matching fence durably records `cleanup_requested`; the persistent worker sees it on its next poll and the GitHub path receives an immediate dispatch. Both destroy only owned resources and publish fresh absence evidence. A failed signal or dispatch does not suppress local cleanup, and the flag itself never marks the lease absent. | P13, P14, P15, P45, P46 |

Each case must exercise a public operation. Assert both the controller result and the fake provider's independent instance state. Expected failures must not be scored as passed paid deployments.

## Metamorphic relations

| ID | Transformation | Required relation |
|---|---|---|
| M01 | Permute offer/source ordering. | Same eligible set and selected distinct offer. |
| M02 | Change only verification labels or irrelevant host prose. | Same eligibility/ranking when the task has no verification constraint. |
| M03 | Duplicate identical offer records. | No new eligible capacity, altered winner, or extra create. Conflicting duplicates trigger uncertainty. |
| M04 | Lower hourly/total/network/bid caps, shorten deadlines, or add a requirement. | Eligibility cannot increase; a previous accepted plan may become blocked. |
| M05 | Increase one offer's known total cost while holding all other facts fixed. | Its rank cannot improve. |
| M06 | Add a strictly more expensive otherwise identical offer. | Existing winner remains selected. |
| M07 | Add a cheaper valid immediately available offer. | New offer wins the cost objective, unless an explicit performance constraint excludes it. |
| M08 | Convert known quantities between equivalent declared units. | Same cost and eligibility; missing/ambiguous units remain blocked. |
| M09 | Remove a decisive source or change its scoped checkpoint/runtime revision. | Plan invalidates or returns to research; it cannot remain authorized. |
| M10 | Add another simultaneously required model. | Previous single-model fit cannot become a stronger fit conclusion. |
| M11 | Advance clock beyond quote/deadline/authorization expiry. | No stale create; owned expired instances enter cleanup. |
| M12 | Replay cancel/destroy/reconcile repeatedly. | No duplicate paid create or unrelated deletion; cleanup stays idempotent. |
| M13 | Lose create response or crash at any persistence boundary. | No unauthorized replacement; all discoverable request-owned instances remain cleanup obligations. |
| M14 | Raise bid while an on-demand occupant is present. | No guaranteed-ready assertion; bidding remains finite and below caps. |
| M15 | Change generic text-generation fixture to a custom typed-decision interface. | Research must adapt probe/recipe; generic chat success cannot pass. |
| M16 | Replace exact-artifact authorization with a base-only alias and remove selection scope. | Plan becomes artifact-selection-required; restoring exact artifact authorization removes only that gate. |
| M17 | Add heartbeat/health polling while advancing time without inference. | Idle cleanup deadline remains unchanged. |
| M18 | Change a candidate's quantization/revision or duplicate its live offer records. | Evidence cannot transfer across variants; sample counts/statistics remain scoped and duplicate-invariant. |
| M19 | Add explicit scoped auto-selection authorization to an otherwise identical ambiguous candidate request. | Candidate-selection question disappears; eligible artifacts remain inside scope and missing paid limits still block create. |
| M20 | Replace a family query with an exact repository/URL plus artifact file. | Research narrows to that candidate/dependencies; alternative menus/model questions disappear while cost/evidence gates remain. |
| M21 | Add the word "official" while leaving artifact/quant/provider unresolved. | Base provenance can narrow, but no community artifact becomes implicitly authorized. An explicit publisher-only restriction narrows candidates further. |
| M22 | Remove the requested exact artifact, then restore it without changing authorization. | Missing-artifact route records whether alternatives need permission; restoration resumes exact-candidate research without another model question. |
| M23 | Change an in-scope auto-selected artifact to another eligible in-scope artifact, then to an out-of-scope one. | First change can replan without model confirmation; second invalidates selection authorization. Both invalidate relevant evidence/quote bindings. |
| M24 | Increase full-attention context/concurrency/cache dtype bytes while holding documented conventional layout fixed. | Corresponding cache term grows by its sourced formula; fit cannot improve. Unsupported architecture changes return to research. |
| M25 | Increase total MoE expert tensors while keeping active parameters per token fixed. | Stored/fully resident weight terms increase; active compute count does not hold memory constant. Documented placement determines per-device terms. |
| M26 | Duplicate a shard reference or change quant format/loaded dtype/runtime residency. | Identical physical files are counted once; format/dtype/residency changes invalidate prior memory estimates and require relevant sources. |
| M27 | Remove a sourced overhead/cache/allocation term or its supporting runtime documentation. | That term becomes unknown and supported fit cannot remain stronger; it never silently becomes zero. |
| M28 | Change only one directional rate across owner tier boundaries. | The tier/cap changes at its declared strict/inclusive boundary; the unchanged cheap direction cannot hide rejection. |
| M29 | Remove a conversion source/divisor, swap directional mapping or replace native rates with inconsistent display values. | Relevant cost/rate becomes unresolved; unsupported conversions cannot preserve paid eligibility. Explicit equivalent sourced units preserve results. |
| M30 | Increase startup/download/benchmark/retention/retry time or traffic with other facts fixed. | Known full task cost cannot fall; unknown terms never become zero. Adding compute/storage diagnostics to an unchanged total cannot increase its counted charge. |
| M31 | Change exact allocation while keeping model files constant, then delete cached files within unchanged allocation. | First change invalidates the quote/plan; second leaves allocated storage rate unchanged and can increase future download exposure. |
| M32 | Extend retention window or remove a measured cache hit/reuse/revision match. | Retention cost cannot decrease; known savings cannot increase; lost decisive measurements make break-even unknown. |
| M33 | Move a same-machine cache plan to another host or destroy only its instance. | Attachment/cache evidence no longer passes; the surviving volume remains separately billed and supervised. |
| M34 | Add only remote WAN latency or change Tailscale route type. | Client evidence changes; engine/loopback results are not rewritten as GPU performance. Changing runtime/workload/cache invalidates matched benchmark evidence. |
| M35 | Add non-ASCII payloads, transport overhead/retries, unrelated scoped traffic, or a counter reset/wrap. | Application/transport/provider observations remain separately labelled; exact byte arithmetic respects scope and cannot manufacture exact billing. |
| M36 | Add contenders or raise a bid without increasing aggregate caps. | Reserved exposure cannot decrease; previously valid race may block; no unreserved create/escalation occurs. |
| M37 | Replay an identical request concurrently from another computer, then change its digest. | Replay attaches to one operation without extra creates; changed digest conflicts/replans. Losing an attempt response never creates an extra contender. |
| M38 | Make the first running contender fail one simultaneous-model probe, or delay loser/volume absence. | Failed contender cannot win; unresolved loser exposure remains reserved and race cannot claim complete cleanup. |
| M39 | Remove local preparation evidence or replace measured machine capability/cost with an old name/note. | Unpaid preparation/discovery is required; speculative machine facts and zero-dollar local cost do not enter the paid plan. |
| M40 | Tighten only the GPU-ready deadline while keeping model/performance evidence and spend caps fixed. | A cost-first bid that can no longer satisfy the deadline becomes ineligible; routing may choose another already authorized mode or report no eligible offer, but cannot relax the deadline or cap. |
| M41 | Change only the inference-performance target or change simultaneous mode to sequential. | Recompute matched evidence/fit and benchmark plan for that exact workload; acquisition urgency and model identity remain unchanged, and sequential evidence cannot satisfy simultaneous acceptance. |
| M42 | Delay or fail cleanup after a benchmark result is complete. | Benchmark output/metrics remain durably available and unchanged; only cleanup state changes to pending/failed and later verified. Delaying the result until cleanup is forbidden. |
| M43 | Stop the child supervisor, initiating host, external guardian, or Vast API/network in turn while keeping the other conditions fixed. | A surviving guardian detects/reconciles failures within its configured response window; when every control path is unavailable, the system makes no bounded-deletion or hard-final-bill claim and keeps cleanup unresolved until fresh provider evidence proves absence. |
| M44 | Add/remove a saved card or increase stopped-disk retention after credit exhaustion while keeping the user-authorized run caps unchanged. | The user authorization does not expand; saved-card autocharges and any post-stop storage/deletion exposure are added explicitly and cannot fulfill the active cleanup obligation. |
| M45 | Terminate the agent, main controller, or local watchdog at every lifecycle transition; independently vary guardian recovery and provider response timing. | The lease remains a durable cleanup obligation until fresh provider reads confirm absence; retries are idempotent, unrelated resources remain untouched, and missed heartbeat alone never reports success. |
| M46 | Remove either cleanup path or deny either path access to the durable lease registry before create; then restore it after a live lease has started. | A missing pre-create acknowledgement blocks create. During a lease, surviving paths retain ownership and retry; when all paths or the registry are down, the system reports pending cleanup and cost exposure until fresh provider reads prove absence. |
| M47 | Replace an external recovery service with a same-host child process, a logged-in Jev/Chrome session, or a scheduled GitHub Actions workflow. | Guardian readiness cannot improve; only separately hosted, registered cleanup paths satisfy P45. |
| M48 | Increase the machine-hour quote, startup allowance, or network allowance while holding the task envelope and owner caps fixed. | The all-in hourly bound cannot fall; a previously rejected cost bound cannot become eligible. |
| M50 | Set `cleanup_requested` before versus after the hard deadline while keeping the fence and provider state fixed. | Before the deadline, remote cleanup starts promptly; without the signal, cleanup waits for the deadline. Either path still requires fresh complete provider inventories before reporting absence. |

These artifact-resolution tests use synthetic model/base/provider names and fake-Hub/file manifests. The example model families in conversation are not hardcoded discovery results or prefilled solutions.

Generated cases must include zero/near-boundary prices, unknown fields, malformed/conflicting units, missing byte/month divisors, exact allocations, retained volumes, cold/warm cache identity, Unicode and transfer-counter resets, equal-cost ties, expired quotes, multiple GPUs, paused instances, partial lists, concurrent clients/race contenders, and controller restarts. Tests require fixed seeds or saved counterexamples for reproducibility. Fake accounting and runtime clocks maintain truth independently of controller reports.

## Private holdout policy

The evaluator stores hidden scenario inputs and expected traces outside this public repository and outside builder working context. Public documentation contains this rubric and runner interface only. The evaluator creates cases before implementation is assessed; builders receive aggregate results and actionable failure categories without the private answer payload.

Never place hidden fixtures in a public GitHub branch, release artifact, CI log, or the test agent's prompt. Reject a claimed hidden run if cases were read, copied, or supplied to the implementer/challenge agent. A fixture visible in `tests/` is an ordinary regression test regardless of its filename.

The sealed evaluator must include unseen combinations of agent/host loss, each guardian failing, shared-registry or credential loss, delayed provider reads, and recovery after service returns. It must also reject a logged-in browser session or scheduled workflow offered as the sole backup. Keep the exact sequence, timing, account data, and expected trace private.

An already named model cannot be scored as a hidden model-identity test. Its minimal-context end-to-end challenge can still test independent research, module routing, billing gates and cleanup. Add separate unseen workload/schema/provider-failure cases for stronger generalization claims.

## Real challenge protocol

Before dispatch, record the merged repository revision, evaluator identity, sealed-case digest, allowed secret handle, and owner authorization with actual hourly/total/runtime/network/start/bid limits. No test agent receives solved deployment commands, required GPU tier, model-specific source links, or the evaluator's expected answer. It receives the task itself, the repository reference, and the configured limits/access.

The agent starts from the repo entry point and calls its router. Its trace must show research routing, actual retrieved publisher/external evidence, exact checkpoint/runtime recipe, live comparison with cost/network/bid details, and a valid bounded plan. Supplying a prefilled model profile to bypass the research stage fails the research challenge.

The actual paid run must:

- Create only a request-owned instance under the approved limits and persist it before use.
- Install the exact requested adapter/base/loader/runtime as applicable.
- Serve valid inference through every requested model's documented API with artifact identity and measured output evidence. Simultaneous requests keep the complete set resident and exercise their declared concurrent profile.
- Destroy every instance created by the challenge, including failed startup/bid/race attempts, and separately delete all request-owned volumes unless a concrete authorized retention window remains active. Confirm provider absence through fresh complete reads; retained resources stay explicit obligations and cannot count as complete teardown.
- Produce a final sanitized report with owner network policy/unit provenance, exact allocations, complete phase/traffic/storage ledger, quote and actual/estimated cost clearly separated, open uncertainties, source provenance, inference evidence and teardown evidence. Benchmark tasks additionally include preparation and separate engine/loopback/Tailscale/checker receipts with scoped byte observations.

Pass requires all of these. If caps are missing, a correct gate is `blocked_missing_limits`, not pass. If current market capacity is absent, result is `blocked_capacity`, not pass. A successful inference with unresolved cleanup is a failed challenge and an active cleanup obligation.

## Release and checkpoint checks

Run the full offline suite and secret sentinel checks in the required CI job named `gates` before requesting GitHub auto-merge. Validate the continuity overlay and inspect the repository's actual `allow_auto_merge`, ruleset/protection, and required-check configuration. Record test counts/results, revision and remaining limitations in the checkpoint. After merge, verify GitHub reports the PR merged and dispatch the challenge using that merged SHA. If repository settings do not permit auto-merge/required checks, record the actual limitation and use the owner's already authorized merge route without inventing successful auto-merge or protection.

The listing workflow must not push snapshots directly to the default branch. Test that scheduled/manual refresh publishes an artifact or a branch/PR and leaves acceptance to `gates` and PR-only progression.

Do not mark paid operation ready until both offline acceptance and the real challenge pass. Preserve failed challenge checkpoints and regression cases without revealing private holdout payloads.

Sequential planning, accounting and recovery are the initial paid release gate. Retained-cache and parallel-race modes remain disabled until their own public/failure-injection/private acceptance and explicitly authorized real challenges pass. Passing a sequential run does not establish those modes. Contract changes and their unresolved provider conventions are documented in [cost-and-benchmark-plan.md](cost-and-benchmark-plan.md).
