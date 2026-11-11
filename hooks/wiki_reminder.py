"""SessionStart: remind Claude of wiki pages flagged in earlier sessions and not updated since.

At most a few lines. Silent when nothing is pending, the project has no wiki,
or anything unexpected happens.
"""

from _common import ROOT, context, read_event, trace

MAX_PAGES = 5

error = None
try:
    import _wiki_flags as flags

    read_event()
    has_wiki = (ROOT / "wiki").is_dir()
    pending = flags.pending() if has_wiki else {}
except Exception as exc:
    has_wiki, pending, error = None, {}, type(exc).__name__

if pending:
    items = sorted(pending.items())
    lines = [f"wiki: {len(items)} page(s) may be out of date after code edits in earlier sessions:"]
    for page, entry in items[:MAX_PAGES]:
        files = list(entry.get("files", []))
        lines.append(f"- {page} ({', '.join(files[:3])}{', ...' if len(files) > 3 else ''})")
    if len(items) > MAX_PAGES:
        lines.append(f"- ... and {len(items) - MAX_PAGES} more")
    lines.append("Mention this to the user once and offer /replicable:wiki-lint (or wiki-ingest for "
                 "the change that touched them).")
    text = "\n".join(lines)
    context("SessionStart", text)
    trace("context", "pending", pages=len(items), chars=len(text))
elif error:
    trace("error", f"exception:{error}")
else:
    trace("skip", "nothing_pending" if has_wiki else "no_wiki")
