# Vast ephemeral runner specification

Status: accepted implementation target. Offer search, evidence review and lease-control components exist in the working tree, but there is not yet a supported end-to-end paid model-run command. A checkpoint must report actual capability separately from the target.

This repository is the authority for its requirements, contracts, implementation, tests, and progression. A consuming project imports the module and references a pinned Git commit. Chat history and an adopter's copied notes are not required to operate it. User task authorization and explicit constraints remain authoritative inputs.

Canonical task: [VBR-0001 / GitHub issue #1](https://github.com/Pukujan/vastai-gpu-broker/issues/1). The issue owns task scope/status; merged default-branch history owns accepted product code and contracts. Checked-in task/checkpoint files are projections of that authority.

## Required outcome

A trusted agent with only a model-hosting task, a checkout, and access to configured credentials follows the module's next actions. The module routes the agent through exact-model research, documented deployment selection, live marketplace comparison, budget checks, installation, real inference, and verified destruction. Every transition is enforced in code. Merely instructing an agent to read documentation is insufficient.

The system is a reusable Python module with a CLI using the same policy engine. A hosted dashboard or MCP wrapper can be added later without moving the decision rules out of the core. The initial adopter needs no separate control website or GitHub database.

## Required behavior

1. Resolve each exact model, checkpoint revision, dependencies, interface, runtime, and license/access prerequisites. Keep all requested models loaded simultaneously unless the task explicitly requests a different mode.
   Apply the artifact-resolution contract below: ambiguous family/base-only requests require researched exact artifact selection; an exact repository/URL plus named artifact or scoped auto-selection authorization proceeds without another model question.
   First present a comparison of exact canonical IDs/revisions/variants, publisher requirements versus external tested evidence/unknowns, and candidate-specific current Vast suitable-offer counts/hourly min/median/mean. Separate rental types and compute/storage/network components; these are observed per-machine hourly samples, not guaranteed prices.
2. Retrieve publisher Hugging Face material first. When a numeric requirement or deployment method is absent, return a research action for external primary sources and matching deployment reports. Preserve the difference between a publisher requirement, an author-reported configuration, and a locally verified measurement.
   Research exact architecture/configuration, total versus active parameters, selected checkpoint/index/shard bytes and quantization format. Report actual artifact sizes and transparent source-backed VRAM/RAM estimates split into resident weights, workload cache, activations/runtime overhead and loading/transient overhead. Unknown terms remain explicit. Formula-derived estimates are neither official requirements nor proven fit; parameter-count rules of thumb alone are insufficient.
3. Never convert unknown facts into claimed requirements. "Smallest configuration evidenced by available research" is distinct from a proven minimum. A GPU with equal VRAM is not automatically equivalent to a tested card.
4. Include unverified and deverified machines and all documented rental types in market comparison. Exclude offers only for task constraints, unsupported behavior, or documented incompatibility, recording the reason.
5. Compare the requested temporary disk allocation, GPU/CPU/RAM/runtime compatibility, current compute and storage cost, network rates and transfer allowance, availability, and relevant measured performance. Do not impose a fixed VRAM cutoff.
6. Prefer the cheapest eligible offer able to satisfy the task's start deadline. Compute total planned cost when the required inputs are known. If performance comparisons use different protocols, expose them separately instead of inventing one performance score.
7. Keep acquisition urgency (how soon a GPU must be ready) separate from inference performance (such as a measured throughput or benchmark deadline). Use only an explicit numeric objective or a saved owner profile; words like "fast" or "cheap" do not invent deadlines, throughput targets, bid increments, or caps. Select among eligible on-demand offers and allowed finite bid policies by the stated objective. Interruptible bids may be paused and higher bids do not override on-demand priority.
8. A user command such as "run it" authorizes only the exact reviewed plan: selected model artifacts and simultaneous/sequential mode, workload, recipe, offer/quote digest, acquisition strategy, deadlines and spend/network/storage/bid limits. If a relevant input or quote changes, show the revised plan and estimate before creating. An existing bounded authorization can cover later steps without repeated approval.
9. Implement interruptible bidding as a finite, capped state machine. A bid is for the machine per hour, not per GPU. An instance that never starts is a failed attempt and must be destroyed.
10. Require explicit paid authorization, hourly/spend/runtime/network limits, and a bid cap when bidding. Existing authorization in the task is sufficient within its concrete limits. Missing limits route to input collection and block create.
11. Persist intent before create, persist instance IDs before handing over access, start an independent supervisor, and reconcile ambiguous create results before allowing another create. Destroy on completion, cancellation, setup failure, interruption timeout, or deadline.
12. Verify the provider no longer reports every owned test instance. A successful delete response or a stopped state alone is insufficient. Retain unresolved cleanup as a failure with a retryable journal entry.
13. Emit an auditable report containing source provenance, candidate/rejection reasons, quote, actual deployment identity, inference result, measured resource/cost data where available, and teardown evidence. Do not call an estimate an invoice.
14. Make the module's adopter contract and checkpoint policy available from the root entry point. Pin requests to the contract version and invalidate paid plans after relevant input/evidence/quote changes.
15. Enforce a configured idle/cold timer in an independent supervisor. Temporary hosting defaults to destruction at its explicit idle deadline; an owner may configure an earlier stop stage and a separate short retention deadline before destruction. Model-process pause, Vast stop, and destruction have different billing effects and must never be conflated. A provisional phrase such as "a few hours" is not an exact timer setting.
16. Start and verify the independent cleanup supervisor before creating a rental, so its persisted startup/cold deadline covers provisioning and installation. When inference/benchmark work completes, durably emit the benchmark result immediately; cleanup verification is a separate follow-up receipt and must not hold the result hostage. Keep benchmark outcome and cleanup outcome as separate states, and retain/notify any pending cleanup obligation.
17. Treat supervisor death as a separate failure case. A paid create requires a durable recovery guardian outside the rented GPU host, armed with the owned lease identity and deadline before access is handed over. Detect missed supervisor heartbeats, reconcile and retry cleanup through the provider API, and alert on unresolved cleanup. Do not claim an absolute spend ceiling or zero ongoing charges under total control-plane/provider failure: Vast's documented create API has no provider-enforced max-lifetime/auto-destroy field. Never send the account API key to the rented instance as a self-delete workaround.
18. Before create, arm two cleanup paths in separate control-host failure domains and publish the fenced lease intent/deadline to a durable registry both can read. The paths must be able to reconcile ambiguous creates, destroy only request-owned instances and volumes, and verify provider-side absence. Persist a separate `cleanup_requested` signal when work completes, fails, is cancelled, or reaches its deadline so remote workers can start recovery promptly; the signal is not proof of absence. A local browser session, Jev browser automation, or a scheduled GitHub workflow alone does not meet this gate. If either required path or the shared registry is unavailable, block create; if recovery later loses every control path, report `CLEANUP_PENDING` and ongoing-cost uncertainty without claiming a hard cutoff. See P45–P46 and A54–A58.

## Artifact resolution and scoped selection authorization

Model identity includes the base/checkpoint, artifact repository/provider, format, quantization variant, exact selected file(s), and resolved revision. A family name, parameter count, "official" qualifier or base repository ID may resolve the base without resolving which weights/quantization artifact will be hosted.

| Task scope | Required research and route | Model question before rent |
|---|---|---|
| Ambiguous family/name | Discover publisher/base and relevant community quantizer repositories and formats. Compare exact candidates with separately sourced requirements/quality/runtime evidence and live candidate-specific Vast pricing. | Ask the user to select an exact artifact; if no candidate is found or identity remains unresolved, ask whether to explore alternatives instead of inventing a replacement. |
| Identified official base, artifact/provider/format still open | Research the official base plus relevant community quantization providers/formats and keep their artifacts separate. "Official" describes the base unless the task expressly restricts artifacts to publisher weights. | Ask for the exact artifact/quantization/format/provider choice unless selection is already authorized. |
| Exact repository/URL plus requested artifact name/file(s) | Research that candidate and its actual dependencies/runtime/requirements only. Resolve the repository revision and artifact files, then continue to its live cost/budget gate. | No broad alternative menu or redundant model confirmation. If the requested artifact is absent, ask whether to explore alternatives unless alternatives are already authorized. |
| Explicit scoped selection permission, such as any compatible 4-bit artifact for the named base, cheapest valid hosting | Research and compare candidates inside the allowed base/quantization/format/provider/runtime scope, then select by the authorized objective and record the exact result. | Proceed without a candidate-selection question. Missing numeric paid limits produce an input-required cost route, not a model-confirmation question. |

All selection/confirmation overrides bind to an explicit scope and its provenance. They cannot authorize a different base, bit depth, provider, format, or objective outside that scope. If no candidate satisfies the permitted scope, return the scoped research/capacity failure and ask about alternatives only when necessary; do not silently broaden it. Temporary source retrieval failure is not proof an artifact does not exist.

Selecting an artifact, permission to auto-select one, and permission to pay are distinct recorded inputs. Exact/scoped model authorization removes only the model-selection gate; evidence, known owner cost/runtime/network/bid caps, live quote and lifecycle gates still apply.

## Sourced resource derivation

Separate publisher requirements, measured artifact facts, formula-derived estimates, externally tested configurations and local runtime measurements. Every derived term carries its formula, source-backed inputs, workload/runtime scope, units and uncertainty. Check the exact model's primary configuration/files and the exact runtime's allocation/placement/cache documentation; an unsupported formula is an unknown term, not a numeric guess.

For MoE, distinguish total stored/resident weights from parameters active per token. Active parameters do not establish weight VRAM/RAM unless a documented expert residency/offload path supports that conclusion. Cache accounting must follow the actual attention/recurrent/hybrid architecture, context, concurrency, dtype and placement. File size is an artifact/storage fact, not automatically the runtime resident memory.

If unresolved terms prevent a supported fit conclusion, route to more research, a permitted alternative, or explicitly bounded validation. A known lower bound alone cannot prove sufficient capacity. Preserve estimates and uncertainties in candidate comparisons and final reports.

## Completion conditions

Offline acceptance and metamorphic tests must pass before enabling paid execution. A paid challenge counts as passed only when the exact requested model serves a valid inference on a real Vast GPU and all instances created by the challenge are verified destroyed. Research completion, a chosen offer, SSH access, an installer exit code, or a mocked inference do not satisfy the challenge.

A missing owner budget is a correct gate result, but it is not a passed deployment challenge. Cleanup uncertainty, unresolved create results, and unavailable market capacity remain explicitly incomplete.

## Contract documents

- [PDD: properties and build order](docs/PDD.md)
- [SDD: interfaces and state transitions](docs/SDD.md)
- [TDD: acceptance and metamorphic tests](docs/TDD.md)
- [Selection policy](GPU_SELECTION_POLICY.md)
- [Checkpoint and adoption authority](docs/checkpoint-policy.md)
- [Current checkpoint](checkpoints/CURRENT.md)

The specification and these contracts change together. A checkpoint cannot claim a feature is ready until the corresponding code and acceptance evidence exist.
