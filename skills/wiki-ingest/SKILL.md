---
name: wiki-ingest
description: Record a source in the project wiki (wiki/) - a merged or open PR, a commit range, a raw note, pasted text, or the current session's lessons - as decisions, domain rules and traps, then regenerate the index, log the change and run the wiki check. Use when the user asks to record, document or remember something in the wiki, after code changes that the wiki describes, and before ending a session that taught something non-obvious.
argument-hint: <PR number or URL | commit range | raw/<file> | text to save | session>
---

# Ingest a source into the wiki

Source: $ARGUMENTS

The wiki holds **intent** (why, and what must hold). Structure (modules,
calls, imports) belongs to graphify: never copy it into a page. Wiki changes
get no human review, so everything you write must be checked against the code
and the source, and `check` must pass.

1. **Orient.** Call the harness `harness_info` tool; `scripts.wiki` is the
   wiki CLI, run as `python3 <scripts.wiki> <command>`. If the project has no
   `wiki/`, stop and suggest `/replicable-harness:setup`. Read
   `wiki/index.md` (not every page) and the `## Project wiki` section of
   `CLAUDE.md`.
2. **Gather the source.**
   - PR: `gh pr view <n> --json title,body,url,files,comments,reviews` and
     `gh pr diff <n>`.
   - Commit range: `git log --format='%h %s%n%b' <range>` and
     `git diff --stat <range>`.
   - `raw/<file>`: read it. Never edit it.
   - Text that exists nowhere else (meeting notes, a pasted thread, a call's
     decision): save it unchanged as `wiki/raw/YYYY-MM-DD-short-title.md`.
     First scan it for secrets and personal data; if you find any, stop and
     ask the user what to remove. It is immutable once committed.
   - `session`: this conversation. Collect only what a future agent would get
     wrong without being told: decisions and their reasons, rules the code
     must uphold, traps (a symptom whose cause was not obvious, and the fix).
     Skip anything readable from the code or git history.
   - No argument: use the current branch's changes against its base
     (`git diff --stat <base>...HEAD`) and treat it like a PR.
3. **Find the pages it touches.** Run `python3 <scripts.wiki> covers <changed
   files>` for the pages describing those files, and pick others from the
   index summaries. Read only those pages.
4. **Decide, item by item,** what the source says that is intent:
   - a choice between alternatives → a decision (`decisions/`); if it replaces
     an earlier one, set the old page's `status: superseded` and
     `superseded_by`
   - a rule the code must uphold, with its edge cases → a domain page
   - a pitfall → a section in `traps.md`: symptom, cause, fix, how to avoid it
   Prefer updating an existing page to creating one. Create pages only with
   `python3 <scripts.wiki> new <type> "<title>" --summary "<line>" --code
   <paths>`.
5. **Verify before writing.** For every claim about the code, confirm it with
   graphify (`graphify explain "<symbol>"`, `graphify query "<question>"`) and
   the line ranges it points to. Don't write what you couldn't verify; say so
   in the report instead. For a broad source, delegate the verification to
   the `graph-explorer` subagent.
6. **Write.** For each page: update the content, `code` (paths it describes),
   `sources` (the PR URL, commit hashes or `raw/` file) and `updated` (today).
   Keep one topic per page, 50-150 lines; split a page that grows past that.
   Link every new page from `overview.md` or a related page with a relative
   Markdown link, so it isn't an orphan. Never use `[[wikilinks]]`.
7. **Finish.**
   - `python3 <scripts.wiki> index`
   - `python3 <scripts.wiki> log ingest "<source>" --line "updated <page>"
     --line "added <page>"` (one `--line` per page)
   - `python3 <scripts.wiki> check`, plus `--base <base branch>` when on a
     branch. Fix every error and re-run until it passes.
8. **Report** one line per page touched, and anything you could not verify.
   Don't commit unless the user asked; when code changed too, the wiki
   changes belong in the same commit.
