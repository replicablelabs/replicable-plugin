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
- **Maintain:** use the `wiki-ingest` skill to record a PR, commits, a note
  or this session's lessons, and `wiki-lint` to verify stale pages against
  the code. Both use the wiki CLI (`scripts.wiki` in the replicable
  `info` tool): `new`, `index` (never edit `index.md` by hand),
  `covers`, `stale`, `log` and `check`, which must pass before you finish.
- **Links:** relative Markdown links, `[Invoices](../domain/invoices.md)`,
  never `[[wikilinks]]`. Refer to code by inline path, `` `src/x.py` ``.
- **Log:** record each change in `log.md` with the CLI's `log` command
  (`## [YYYY-MM-DD] <op> | <subject>`, then one `- ` line per page).
- **Raw sources:** `raw/YYYY-MM-DD-short-title.md` files are immutable once
  committed; corrections go into pages. Check them for secrets and personal
  data before committing.
- **Keep pages true:** when you change code listed in a page's `code`, update
  that page in the same change (or, if it is still right, bump `updated`).
  Wiki changes get no human review: CI runs `check --base <PR base>` and
  fails on code changes that leave their pages untouched.
