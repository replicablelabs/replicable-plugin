## Project wiki

`wiki/` holds this project's *intent*: the decisions taken, the domain rules
the code must uphold and the known traps. Structure (modules, calls, imports)
belongs to graphify; never copy it into the wiki.

- **Query:** before working on an area, read `wiki/index.md`, then only the
  pages whose summary is relevant. Say which pages you relied on.
- **Pages:** `overview.md`, `decisions/NNNN-short-title.md`,
  `domain/<topic>.md` and `traps.md` (one section per trap). Each starts with
  frontmatter: `type`, `summary` (at most 120 characters), `code` (repository
  paths it describes), `sources` (PRs, issues, commits, `raw/` files) and
  `updated` (YYYY-MM-DD). One topic per page, 50-150 lines.
- **Tooling:** the wiki CLI is `scripts.wiki` in the harness `harness_info`
  tool. Create pages with `new <type> "<title>"`, regenerate the index with
  `index` (never edit `index.md` by hand), list outdated pages with `stale`,
  and run `check` before finishing.
- **Links:** relative Markdown links, `[Invoices](../domain/invoices.md)`,
  never `[[wikilinks]]`. Refer to code by inline path, `` `src/x.py` ``.
- **Log:** append one entry per change to `log.md`:
  `## [YYYY-MM-DD] <ingest|update|lint> | <subject>`, then `- ` lines naming
  the pages touched.
- **Raw sources:** `raw/YYYY-MM-DD-short-title.md` files are immutable once
  committed; corrections go into pages. Check them for secrets and personal
  data before committing.
- **Keep pages true:** when you change code listed in a page's `code`, update
  that page in the same change (or, if it is still right, bump `updated`).
  Wiki changes get no human review: CI runs `check --base <PR base>` and
  fails on code changes that leave their pages untouched.
