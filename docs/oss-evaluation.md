# Open-source project evaluation

Reviewed 2026-09-28 at upstream commit `cd59bbd47165fd7c0358a5c3b63cd51108a6a53e`. This is a scoped review of selected repository documentation and evidence artifacts, not an independent audit of all code or benchmark claims.

## TurboFit

Evaluated project: [SouthpawIN/turbofit](https://github.com/SouthpawIN/turbofit), whose repository lists an MIT license. Its README describes a Hermes Agent provider and adaptive **local** inference runtime: hardware inventory, model/profile selection, artifact acquisition, local serving, and an adaptive residency ladder. It also describes a benchmark stage that records inference output/performance and resource evidence.

The repository does contain a durable, project-authored evidence corpus—not just the short model list. Relevant files include [`hardware-tier-report.json`](https://github.com/SouthpawIN/turbofit/blob/cd59bbd47165fd7c0358a5c3b63cd51108a6a53e/references/hardware-tier-report.json), hardware/model ranking and tournament files, benchmark suites, and dated artifacts under [`references/results`](https://github.com/SouthpawIN/turbofit/tree/cd59bbd47165fd7c0358a5c3b63cd51108a6a53e/references/results). For example, the tier report marks several capacities as `catalog-candidates-only` with no measured winner, while its 48 GB tier includes a measured profile. The historical records are therefore useful for finding prior model/quant/hardware/context/runtime combinations and performance observations, but they are uneven: some are candidates or inferred estimates, some runs failed, and results are tied to particular hosts and workloads. They are not a universal best-fit table, a current Vast price feed, or official Hugging Face minimum requirements. Raw machine-local campaign attempts are explicitly excluded from the release tree.

### Recommendation

Do not adopt TurboFit as this broker's dependency or decision authority. Its documented role does not cover Vast offer search, network/storage price accounting, interruptible bids, paid-instance ownership, cancellation, or verified teardown. Its README's model ladder is a curated selection and uses a general `model size + KV cache` fit rule; those claims do not satisfy this project's requirement to research the exact Hugging Face artifact, runtime, workload, and resource terms from scoped primary evidence. The broker must not let that ladder silently substitute a model or quantization.

There are three ideas worth borrowing without importing its model choices: (1) consult prior hardware/benchmark records as historical evidence with exact provenance, (2) run hardware detection and a readiness check **after** the exact deployment is placed on a Vast machine, and (3) collect engine/serving benchmark evidence with exact artifact, runtime, and workload identity. Historical TurboFit results can inform a candidate comparison, but may not establish the requested artifact's minimum requirement or override fresh model/runtime research. Those checks should be implemented through this project's provider-neutral recipe and evidence contracts. TurboFit can be revisited as an optional runtime adapter only for an explicitly selected Hermes/compatible local-runtime deployment after a separate code/API review.

For this project, build the controller and benchmark harness on the available PC/Linux/Mac first; run GPU-specific loading and inference on the rented host, using a local endpoint there for host-performance measurements. If the real agent client is remote, measure its Tailscale experience separately. TurboFit does not replace either side of that cost-aware workflow.

### Boundaries of this review

- Repository README, listed license, selected ranking/benchmark documents, and representative JSON result artifacts were inspected; source code, CI, pinned dependency graph, and the full benchmark corpus were not audited.
- Statements about TurboFit's features and model measurements above describe what its repository claims. They are not treated as independent validation of model fit, quality, or speed.
- No TurboFit code was copied or executed.
