---
name: wiki-lint
description: Audit the project wiki (wiki/) - fix structural problems found by the wiki check, then verify stale pages' claims against the code with graphify and correct or re-date them, and report gaps and contradictions. Use when the user asks to lint, audit or refresh the wiki, or when `wiki check` or the session-start reminder reports stale pages.
argument-hint: "[all | <page path>]"
---

# Lint the wiki

Scope: $ARGUMENTS (default: stale pages only; `all` checks every page with
`code`; a path checks one page)

1. **Orient.** Call the harness `harness_info` tool; `scripts.wiki` is the
   wiki CLI, run as `python3 <scripts.wiki> <command>`. If the project has no
   `wiki/`, stop and suggest `/replicable-harness:setup`.
2. **Structure.** Run `python3 <scripts.wiki> check`. Fix each error:
   broken links (find where the page went), `[[wikilinks]]` (rewrite as
   relative Markdown links), `code` paths that no longer exist (find the new
   location with `git log --follow --name-status -- <old path>` or graphify,
   or drop the path), bad file names (rename and fix links to the page), and
   an outdated index (`python3 <scripts.wiki> index`). Never edit `raw/`: if
   a raw file was modified, restore it with `git checkout <first commit> --
   <file>` after asking the user.
3. **Pick pages.** `python3 <scripts.wiki> stale` lists pages whose code
   changed after their `updated` date. With `all`, take every page that has
   `code`.
4. **Verify each page against the code.** For each page:
   - `git log --format='%h %ad %s' --date=short --since=<updated> --
     <code paths>` shows what changed since it was last checked.
   - For each claim, check it with graphify (`graphify explain "<symbol>"`,
     `graphify query "<question>"`, `graphify affected "<file>"`) and the line
     ranges it points to. Delegate to the `graph-explorer` subagent when a
     page covers a lot of code.
   - Still true: bump `updated` to today. Wrong: correct it, add the commit
     or PR that changed it to `sources`, bump `updated`. Can't tell: leave
     the claim, add a `> [!WARNING]` callout saying what is unverified, and
     don't bump `updated`.
5. **Look across pages.** Report, don't silently resolve: pages that
   contradict each other, decisions superseded in practice but still
   `accepted`, orphans the check warned about (link them from a related page
   when the link is obvious), and pages over 150 lines (split them by topic).
   Optionally run `graphify god-nodes` and list heavily used code that no page
   covers (`python3 <scripts.wiki> covers <file>` prints nothing) as
   candidates for `wiki-ingest`; don't create those pages here.
6. **Finish.**
   - `python3 <scripts.wiki> index`
   - `python3 <scripts.wiki> log lint "<scope>" --line "verified <page>"
     --line "corrected <page>: <what>"` (one `--line` per page changed)
   - `python3 <scripts.wiki> check --strict`; fix what you can and explain
     the warnings that remain.
7. **Report** pages verified, corrected and left unverified, and the
   cross-page findings. Don't commit unless the user asked.
