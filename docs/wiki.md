# Project wiki (design)

> **Status: partly implemented.** This page specifies what will be built;
> [Rollout](#rollout) lists the order and marks what exists. Anything not
> marked done there does not exist in the plugin yet.

Each project gets a wiki that **agents read first and humans can browse**,
with Obsidian as the viewer and GitHub as a fallback. It follows the pattern
in Andrej Karpathy's
[LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
immutable raw sources, LLM-maintained pages, and a schema that tells the LLM
how to maintain them. Claude Code maintains the wiki, using this plugin's
skills, hooks and CLI.

## Two halves: structure and intent

| | Structure: *what the code is* | Intent: *why, and what must hold* |
| --- | --- | --- |
| Owner | graphify | This wiki |
| Content | Modules, calls, imports, communities, hubs | Decisions, domain rules, constraints, traps |
| Produced by | Regenerated from the code (`graphify update`, `graphify export wiki`) | Maintained page by page by Claude Code, reviewed like code |
| Location | `graphify-out/` | `wiki/` |

The intent wiki never duplicates structure. It names the code it concerns
(see [Frontmatter](#frontmatter)), and agents use graphify for everything
structural.

## Distribution and instances

```
replicable-claude (this plugin, installed once per developer)
   skills, hooks, CLI, templates
        │  /replicable-harness:setup  installs or upgrades
        ▼
each project repository (committed with the project)
   CLAUDE.md                            graphify section + wiki schema section
   .claude/settings.json                plugin enabled for the team + graphify's hooks
   .claude/replicable-harness.json      test command, harness_version
   wiki/                                this wiki
   graphify-out/                        graphify's structure graph
```

Mechanisms live in the plugin; knowledge lives in the repository. A plugin
update improves every project; each project's wiki stays with its code and its
history.

## Layout

```
wiki/
  index.md          catalogue, generated from frontmatter (never hand-edited)
  log.md            append-only record of wiki changes
  overview.md       what the system is for, in one screen
  decisions/        ADR-style records: 0001-postgres-over-mongo.md
  domain/           rules that must hold: invoices.md
  traps.md          known pitfalls, each with the symptom and the fix
  raw/              immutable source notes (see Raw sources)
  .obsidian/        shared Obsidian settings (personal ones gitignored)
```

### Page types

| `type` | Folder | Holds | Extra fields |
| --- | --- | --- | --- |
| `overview` | `wiki/` | Purpose, users, the main flows, links to the other pages | — |
| `decision` | `decisions/` | Context, decision, consequences, alternatives considered | `status`: `proposed`, `accepted`, `superseded` or `deprecated`; `superseded_by` |
| `domain` | `domain/` | Rules the code must uphold, with examples and edge cases | — |
| `trap` | `traps.md` (one section per trap) | Symptom, cause, fix, how to avoid it | — |

## Frontmatter

Every page except `index.md`, `log.md` and `raw/` starts with:

```yaml
---
type: domain
summary: Invoices are immutable once issued; corrections are credit notes.
code:
  - src/billing/invoice.ts
  - src/billing/credit_note.ts
sources:
  - https://github.com/org/repo/pull/123
  - raw/2026-11-03-billing-call.md
updated: 2026-11-09
tags: [billing]
---
```

| Field | Required | Purpose |
| --- | --- | --- |
| `type` | Yes | One of the page types above |
| `summary` | Yes, at most 120 characters | The line `index.md` shows. Agents decide from it whether to open the page |
| `code` | Recommended | Repository paths the page describes. They connect the page to graphify's graph and make staleness checks possible |
| `sources` | Recommended | PRs, issues, commits or `raw/` files the content came from |
| `updated` | Yes | The date the content was last checked against its code and sources |
| `tags` | No | For Obsidian's tag views; ignored elsewhere |

Obsidian shows frontmatter as Properties; GitHub shows it as a table.

## Obsidian and GitHub conventions

One syntax that renders well in both:

| Element | Convention |
| --- | --- |
| Links between pages | Relative Markdown links, `[Invoices](../domain/invoices.md)`. Obsidian counts them for backlinks and the graph view; GitHub renders them. **No `[[wikilinks]]`**: GitHub doesn't render them |
| File names | `kebab-case.md`, no spaces. Decisions are numbered: `0007-short-title.md` |
| Code references | Inline code paths, `` `src/billing/invoice.ts` ``. The vault is `wiki/`, so links to code would not resolve in Obsidian |
| Callouts | `> [!NOTE]`, `> [!WARNING]` (rendered by both) |
| Diagrams | Mermaid code blocks (rendered by both) |

Open `wiki/` as the Obsidian vault. Setup commits a shared
`wiki/.obsidian/app.json` so links created in Obsidian follow the conventions:

```json
{ "useMarkdownLinks": true, "newLinkFormat": "relative", "alwaysUpdateLinks": true }
```

and gitignores the personal files (`wiki/.obsidian/workspace*.json`).

| Key | Obsidian default | Here |
| --- | --- | --- |
| `useMarkdownLinks` | `false` (creates `[[wikilinks]]`) | `true`: create Markdown links |
| `newLinkFormat` | `"shortest"` (other options: `"relative"`, `"absolute"`) | `"relative"`: paths relative to the current file, as GitHub needs |
| `alwaysUpdateLinks` | `false` (asks first) | `true`: renaming a page updates links to it |

Obsidian writes a key to `app.json` only once it differs from the default, so
these three keys are the whole file. (Key names from a
[community settings reference](https://faerimagic.github.io/faeri-obsidian-vaults/App-Store/Apps/Productivity/Obsidian/Obsidian-Settings);
recheck in a real vault during implementation.)

### Page size

One page per topic (an area's rules, or one decision), 50–150 lines. Agents
read a page in full, so large pages cost tokens on content that isn't needed,
and very small pages mean many hops and an index too long to scan. `wiki
check` warns above 150 lines.

## Raw sources

| Source | Where it lives |
| --- | --- |
| Code and commits | Git. Never copied |
| PRs and issues | GitHub. Linked from `sources`; `wiki-ingest` reads them with `gh` |
| Anything that exists nowhere else: meeting notes, design discussions, decisions made in calls, pasted threads | `wiki/raw/`, committed |

Rules for `wiki/raw/`:

- **Immutable once committed.** Corrections go into wiki pages, never back into
  raw, so every claim can be traced to an unaltered source.
- **Text only**, named `YYYY-MM-DD-short-title.md`. Large binaries go to Git
  LFS or stay external, linked from `sources`.
- **Checked before committing.** Raw notes can contain personal data or
  secrets, and git history keeps them. The CLI warns about likely secrets.

## `index.md` and `log.md`

`index.md` is **generated** by the CLI from frontmatter, grouped by type, one
line per page:

```markdown
## Domain
- [Invoices](domain/invoices.md): Invoices are immutable once issued; corrections are credit notes.
```

Generating it means it can't drift from the pages; the check fails if it is
out of date.

`log.md` is append-only, newest last, one entry per change, in a format that
plain tools can parse:

```markdown
## [2026-11-09] ingest | PR #123 billing credit notes
- updated domain/invoices.md, added decisions/0007-credit-notes.md
```

## Operations

| Karpathy's operation | Here | Implemented as |
| --- | --- | --- |
| **Ingest** | Turn a source (a merged PR, a `raw/` note, the current session's lessons) into page updates, then append to `log.md` and regenerate `index.md` | `wiki-ingest` skill (uses judgement) + CLI (index, check) |
| **Query** | Read `index.md`, then only the pages needed; cite them | No new code: the schema section in `CLAUDE.md` tells agents to do this |
| **Lint** | Find structural problems, then claims the code no longer supports | CLI `check` (deterministic), then the `wiki-lint` skill (judgement, using graphify) |

## CLI

A standard-library Python script in the plugin, run as
`python3 <plugin>/harness/wiki.py <command>`. Skills get the path from
`harness_info`; hooks and CI call it directly.

| Command | Does |
| --- | --- |
| `check [--strict] [--base <ref>]` | Runs every check below. Exit 1 on errors; warnings fail only with `--strict`. `--base` compares against a git ref (CI passes the PR's base) to catch code changes that left their pages untouched |
| `index` | Regenerates `index.md` from frontmatter |
| `stale` | Lists pages whose `code` changed after their `updated` date |
| `new <type> <title> [--summary <text>] [--code <path>...]` | Creates a page from the template with valid frontmatter (a trap is appended to `traps.md` as a new section) and regenerates `index.md` |

The CLI reads frontmatter with a small YAML subset (plain and quoted values,
flow and block lists), which is what Obsidian writes. A value containing `: `
must be quoted. Staleness compares dates, so a code change committed on the
same day as `updated` doesn't count.

Checks:

| Check | Level |
| --- | --- |
| Frontmatter present, valid, with the required fields | Error |
| Relative links resolve | Error |
| `[[wikilinks]]` used | Error |
| Files listed in `code` exist | Error (the code moved or was deleted) |
| `index.md` matches the frontmatter | Error |
| A `raw/` file was modified after its first commit | Error |
| File names follow the convention | Error |
| A change set (for example a PR) modifies a page's `code` files without touching the page | Error in CI (`check --base <ref>`) |
| Page is stale: `git log` shows a `code` file changed after `updated` | Warning |
| Page longer than 150 lines | Warning |
| Orphan page: no inbound links other than from the index | Warning |
| Likely secret in `raw/` | Warning |
| Superseded decision without `superseded_by` | Warning |

## Hooks

| Hook | Event | Does |
| --- | --- | --- |
| Staleness flag | PostToolUse (Edit, Write) | When the edited file appears in some page's `code`, tells Claude which pages may now be wrong and suggests `wiki-ingest` before finishing. Also records them in the plugin's data directory |
| Stale-page reminder | SessionStart | Lists pages flagged in earlier sessions and not yet updated (at most a few lines) |

Learnings are captured through `wiki-ingest` (for example, `wiki-ingest
session` before ending a session), not automatically at SessionEnd. By the
time SessionEnd runs the model can no longer act, so a hook there could only
save a reminder, which the SessionStart hook above already covers.

## Setup and upgrades

`/replicable-harness:setup` creates or upgrades, after showing a diff and
asking. It runs `harness/scaffold.py`, which prints the plan and diffs by
default and writes only with `--apply`:

| File | Created | On upgrade |
| --- | --- | --- |
| `wiki/` skeleton: `overview.md`, `log.md`, `index.md`, `decisions/`, `domain/`, `raw/` (each folder with a `.gitkeep`) | When missing | Never touched |
| `wiki/.obsidian/app.json` | When missing | The three link keys are set; any other keys Obsidian wrote are kept |
| `CLAUDE.md` schema section between `<!-- replicable-harness:wiki v1 -->` and `<!-- /replicable-harness:wiki -->` (template: `harness/templates/claude-wiki-section.md`) | Appended, or a new `CLAUDE.md` | Content between the markers replaced |
| `.gitignore` block between `# replicable-harness (managed)` and `# /replicable-harness` (Obsidian's personal files, `graphify-out/cache/`) | Appended | Block replaced |
| `.github/workflows/wiki.yml` (see [CI](#ci)) | Yes | Replaced: the plugin owns it |
| `harness_version` in `.claude/replicable-harness.json` | Yes | Set to the plugin's version; other keys kept |

Upgrades compare the repository's `harness_version` with the plugin's
version (`.claude-plugin/plugin.json`), and refuse when the repository is
newer than the installed plugin. They **only replace content between the
plugin's markers and files the plugin owns**. They never modify wiki pages or
`raw/`. Version-specific steps go in `MIGRATIONS` in `scaffold.py` and run in
version order for every version between the repository's and the plugin's,
so a repository several versions behind upgrades in steps.

## Privacy and the inference proxy

- Claude Code runs through the organisation's inference proxy, configured in
  user or managed settings (`ANTHROPIC_BASE_URL` and credentials). **Never in a
  committed file.** Setup checks that a proxy is configured and says so; it
  never writes the setting.
- Maintaining the wiki therefore sends nothing anywhere Claude Code doesn't
  already send it.
- graphify stays code-only by default. Its LLM steps (community names, document
  extraction) are optional and, if used, must run through the same proxy with
  an explicit `--backend` (`claude` with `ANTHROPIC_BASE_URL`, or `openai` with
  `OPENAI_BASE_URL`), never `claude-cli`, and with no other provider keys in
  the environment.

## CI

A project's CI (`.github/workflows/wiki.yml`, installed by setup) runs the
deterministic checks by cloning this plugin at the tag `v<harness_version>`
and running `wiki.py check --base origin/<PR base>` on pull requests (`check`
alone on pushes). Each plugin release therefore needs a matching `v` tag. The
base branch name reaches the script through an environment variable, never
interpolated into it. The history checks need full history, so the checkout uses
`fetch-depth: 0`; on a shallow clone `check` warns and skips them.
**Wiki changes need no human review:** they merge when the check
passes. That makes the check the safety net, which is why a code change that
leaves its pages untouched is an error in CI rather than a warning.

## graphify's Obsidian export

`graphify export obsidian` turns graphify's *code graph* into an Obsidian
vault: one note per node (functions, classes, files; 357 notes for a small
repository) plus a `graph.canvas`, linked by calls and imports. It shows
structure, not intent. It is **not committed and not merged into `wiki/`**: it
is regenerated whole, uses `[[wikilinks]]`, and has file names with spaces and
brackets. To browse structure in Obsidian, export it locally into a separate
vault; `graphify-out/graph.html` serves the same purpose without Obsidian.

## Rollout

1. CLI: `check`, `index`, `stale`, `new`, with tests. **Done:** `harness/wiki.py`
2. Setup: scaffold, `CLAUDE.md` schema section, Obsidian settings, `harness_version` and upgrades. **Done:** `harness/scaffold.py`, run by the `setup` skill
3. Skills: `wiki-ingest`, `wiki-lint`
4. Hooks: staleness flag, stale-page reminder
5. Trial on `replicable-labs`, then on other projects

## Decisions

- **Page size:** one page per topic, 50–150 lines (see [Page size](#page-size)).
- **Review:** no human review for wiki changes; `check` in CI is the gate.
- **graphify's Obsidian export:** local only, separate vault, never committed.
