# Content system adoption

This repository uses the public Content Generation Modules helper at version
`0.5.7`, pinned to commit
[`c069613ca8b3e02bcf5aba1960160583537f8a3a`](https://github.com/Pukujan/content-generation-modules/tree/c069613ca8b3e02bcf5aba1960160583537f8a3a).
The adapter lives in `.content-system/`. It records the project brief, brand
language, visual contract, asset provenance, review rubric, and all eight
module IDs. The helper itself is checked out at that immutable revision for
validation; its unrelated history, assets, and implementation files are not
copied into the broker.

At agent start, `AGENTS.md` includes the exact always-on system block from the
pinned helper's `docs/writing-routing.json`. It requires `human-sounding-writing`
for human-facing work, sends README and product-entry pages to
`writing-direction`, and sends new media filenames and legends to
`human-output-naming`. The matching copy is stored at
`.content-system/agent-system-block.txt`; validation checks both copies against
the pinned source.

## Checks

The `gates` job checks the adapter, system block, asset hash and source record,
then runs the pinned helper's full adapter validator and HSW automation check.
It does not score writing quality or establish that a product claim is true.
Those claims remain tied to project evidence and human review.

Run the same checks locally from the repository root after checking out the
pinned helper to `.cgm`:

```powershell
git clone https://github.com/Pukujan/content-generation-modules.git .cgm
git -C .cgm checkout c069613ca8b3e02bcf5aba1960160583537f8a3a
python scripts/validate_cgm_adoption.py --helper-root .cgm
python .cgm/scripts/validate_content_system.py --root .cgm --adapter .content-system --project-root .
python .cgm/scripts/verify_hsw_applied.py --root .cgm
```

The architecture image is a code-rendered helper graphic rather than a
narrative image. Its renderer, labels, intended placement, review decision,
dimensions, and hash are recorded in `.content-system/prompts/` and
`.content-system/asset-manifest.json`. It presents the target workflow as
planned so readers do not mistake a diagram for implementation or paid-run
evidence.
