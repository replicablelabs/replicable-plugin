"""Pages flagged as possibly out of date, kept in the plugin's data dir (never in the repository).

    {"wiki/domain/invoices.md": {"files": ["src/billing.py"], "since": 1762700000.0}}

A flag is set when code a page describes is edited, cleared when the page is
edited, and dropped once the page file is newer than the flag.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Tuple

from _common import ROOT, STATE

import wiki  # noqa: E402  (harness/ is on sys.path via _common)

FILE = STATE / "wiki-flags.json"


def load() -> Dict[str, dict]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(flags: Dict[str, dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(flags, indent=1, sort_keys=True))
    tmp.replace(FILE)


def flag(rel: str) -> List[Tuple[str, List[str]]]:
    """Flag the pages covering ``rel``; return only (page, files) that are newly flagged."""
    flags, new = load(), []
    for page, _ in wiki.covered_by(wiki.load(ROOT), [rel]):
        key = f"{wiki.WIKI_DIR}/{page.rel}"
        entry = flags.setdefault(key, {"files": [], "since": time.time()})
        if rel not in entry["files"]:
            entry["files"].append(rel)
            new.append((key, entry["files"]))
    if new:
        save(flags)
    return new


def clear(rel: str) -> None:
    flags = load()
    if flags.pop(rel, None) is not None:
        save(flags)


def pending() -> Dict[str, dict]:
    """Flags whose page still exists and was not modified after it was flagged."""
    flags = load()
    keep = {}
    for key, entry in flags.items():
        path = ROOT / key
        try:
            if path.stat().st_mtime <= float(entry.get("since", 0)):
                keep[key] = entry
        except (OSError, TypeError, ValueError):
            continue
    if keep != flags:
        save(keep)
    return keep
