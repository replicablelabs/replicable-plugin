"""PostToolUse(Edit|Write): name the wiki pages an edit may have made wrong.

When the edited file is in some page's `code` (see `wiki.py covers`), tell
Claude which pages to check and record them, so the SessionStart reminder can
bring them up again if the session ends first. Editing a flagged page clears
its flag. Silent when the project has no wiki, and on anything unexpected.
"""

from pathlib import Path

from _common import ROOT, context, read_event

try:
    import _wiki_flags as flags

    event = read_event()
    tool_input = event.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path or not (ROOT / "wiki").is_dir():
        raise SystemExit(0)
    try:
        rel = Path(file_path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        raise SystemExit(0)  # outside this project
    if rel.startswith("wiki/"):
        flags.clear(rel)
        raise SystemExit(0)
    new = flags.flag(rel)
except SystemExit:
    raise
except Exception:
    raise SystemExit(0)  # helper hook: fail open

if new:
    pages = ", ".join(page for page, _ in new)
    context("PostToolUse",
            f"wiki: {rel} is described by {pages}. Check that the page still holds; update it (or bump `updated` "
            "if it does) in this same change, for example with the wiki-ingest skill, before you finish. "
            "CI fails a PR that changes a page's code without touching the page.")
