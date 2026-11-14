# xp-backstage (devops)

The scaffolder that fuses a pinned xp-platform tag with pinned xp-pricer-templates pricers
into a standalone generated repo. No C++.

- `generate.sh <name> <platform-tag> <pricers-tag> <method...>` — the runnable generator.
- `copier.yml` — the declarative golden-path equivalent (`_preserve_symlinks: false`, pin via
  `--vcs-ref`).
- `catalog-info.yaml` + `quant-pricer-template.yaml` — the Backstage Create-page template.

A generated repo gets: a real `platform/` at the pinned tag, a 3-line root
`CMakePresets.json` (include only), `.copier-answers.yml` recording both tags, `renovate.json`,
and a CODEOWNERS splitting platform (build-eng) from pricers (quants).
