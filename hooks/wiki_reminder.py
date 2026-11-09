"""SessionStart: remind Claude of wiki pages flagged in earlier sessions and not updated since.

At most a few lines. Silent when nothing is pending, the project has no wiki,
or anything unexpected happens.
"""

from _common import ROOT, context, read_event

MAX_PAGES = 5

try:
    import _wiki_flags as flags

    read_event()
    pending = flags.pending() if (ROOT / "wiki").is_dir() else {}
except Exception:
    pending = {}

if pending:
    items = sorted(pending.items())
    lines = [f"wiki: {len(items)} page(s) may be out of date after code edits in earlier sessions:"]
    for page, entry in items[:MAX_PAGES]:
        files = list(entry.get("files", []))
        lines.append(f"- {page} ({', '.join(files[:3])}{', ...' if len(files) > 3 else ''})")
    if len(items) > MAX_PAGES:
        lines.append(f"- ... and {len(items) - MAX_PAGES} more")
    lines.append("Mention this to the user once and offer /replicable-harness:wiki-lint (or wiki-ingest for "
                 "the change that touched them).")
    context("SessionStart", "\n".join(lines))
