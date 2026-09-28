# Scheduled offer refresh

`.github/workflows/sync-offers.yml` refreshes the read-only `data/latest.json`
snapshot on a schedule or by manual dispatch. The snapshot is committed to the
dedicated `automation/vast-offer-snapshot` branch and proposed through one
reused pull request targeting `main`. The workflow never pushes the snapshot
directly to `main`; normal `gates` acceptance and repository merge controls own
the decision to accept it.

The refresh calls Vast's documented `POST /api/v0/bundles` endpoint once for
each rental type. It does not filter verification, rented, or rentable status.
Every result type that reaches `VAST_OFFER_LIMIT` is marked possibly truncated,
because the current endpoint reference documents a maximum result limit but no
cursor/offset pagination contract. Status counts are recorded as observations,
not proof that all marketplace offers were returned.

The Vast API key is supplied only to the refresh process. It is not passed to
the PR action or gates workflow. The snapshot writer drops credential-shaped
fields and redacts any exact API-key occurrence before writing the file. Refresh
failures leave the previous snapshot intact and print only the error class.

## CI trigger behavior

GitHub does not start new `pull_request` or `push` workflows from events caused
by the repository's `GITHUB_TOKEN`; this prevents recursive workflow runs. The
snapshot workflow therefore explicitly dispatches `.github/workflows/ci.yml`
(the workflow whose required job is named `gates`) against the PR branch and
waits for that run to finish. Before dispatch, it records existing dispatch run
IDs for the exact PR SHA. It then accepts exactly one new run whose event is
`workflow_dispatch` and whose head SHA equals the PR SHA. It fails if none or
multiple matching runs appear; it never chooses a run by branch name alone.
This explicit `workflow_dispatch` is permitted for `GITHUB_TOKEN` and runs the
checked-out PR revision. The job still
requires repository branch protection/rulesets to require the `gates` check;
workflow code cannot create that policy. Confirm the dispatched run is visible
against the pull request's head commit in the repository's Checks UI. If it is
not, configure a GitHub App or narrowly scoped PAT for the automation so its
PR update emits normal `pull_request` events, or dispatch the check manually.

No workflow can infer that GitHub branch protection is enabled from a green CI
run. Until the repository's required-check configuration is verified, a green
run is evidence for that revision but not evidence that merging is blocked
without it.
