"""PostToolUse(Edit|Write): name the wiki pages an edit may have made wrong.

When the edited file is in some page's `code` (see `wiki.py covers`), tell
Claude which pages to check and record them, so the SessionStart reminder can
bring them up again if the session ends first. Editing a flagged page clears
its flag. Silent when the project has no wiki, and on anything unexpected.
"""

from pathlib import Path

from _common import ROOT, context, read_event, trace


def skip(reason: str) -> None:
    trace("skip", reason)
    raise SystemExit(0)


try:
    import _wiki_flags as flags

    event = read_event()
    tool_input = event.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        skip("no_path")
    if not (ROOT / "wiki").is_dir():
        skip("no_wiki")
    try:
        rel = Path(file_path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        skip("outside_root")
    if rel.startswith("wiki/"):
        flags.clear(rel)
        skip("page_edited")
    new = flags.flag(rel)
except SystemExit:
    raise
except Exception as exc:
    trace("error", f"exception:{type(exc).__name__}")
    raise SystemExit(0)  # helper hook: fail open

if new:
    pages = ", ".join(page for page, _ in new)
    text = (f"wiki: {rel} is described by {pages}. Check that the page still holds; update it (or bump `updated` "
            "if it does) in this same change, for example with the wiki-ingest skill, before you finish. "
            "CI fails a PR that changes a page's code without touching the page.")
    context("PostToolUse", text)
    trace("context", "flagged", pages=len(new), chars=len(text))
else:
    trace("skip", "not_covered_or_already_flagged")
